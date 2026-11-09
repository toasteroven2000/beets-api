"""Modify with a Preview first: `beet modify` split across two HTTP requests."""

import logging
from concurrent.futures import ThreadPoolExecutor

from beets import ui, util
from beets.exceptions import UserError
from beets.library import Album, Item
from beets.ui.commands.modify import _check_modify_operations, modify_parse_args
from beets.util import functemplate
from fastapi import HTTPException

from .library import fingerprint, forget_cached_paths, json_value, to_json
from .store import Store

log = logging.getLogger(__name__)

KIND = "modification"
PROTECTED_FIELDS = {"id", "path"}


def _parse_changes(album: bool, set_: dict[str, str], delete: list[str]):
    """Use `beet modify`'s own argument parser, so field rules match the CLI."""
    args = [f"{k}={v}" for k, v in set_.items()] + [f"{f}!" for f in delete]
    rest, mods, dels = modify_parse_args(args, is_album=album)
    if rest:
        raise HTTPException(422, f"invalid field names: {rest}")
    if not mods and not dels:
        raise HTTPException(422, "no changes requested")
    if protected := PROTECTED_FIELDS & (set(mods) | set(dels)):
        raise HTTPException(422, f"cannot modify: {sorted(protected)}")
    try:
        _check_modify_operations(Album if album else Item, mods)
    except UserError as exc:
        raise HTTPException(422, str(exc))
    return mods, dels


def _modify_object(obj, model, mods, dels) -> None:
    """The same steps as beets' `modify_items`, for one object."""
    for key, mod in mods.items():
        parsed = model._parse(key, obj.evaluate_template(functemplate.template(mod.value)))
        obj[key] = mod.apply(obj, key, parsed)
    for field in dels:
        try:
            del obj[field]
        except KeyError:
            pass


def _diff(before: dict, after: dict) -> dict:
    return {
        key: {"old": before.get(key), "new": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if key != "mtime" and before.get(key) != after.get(key)
    }


def _entry(label: str, before: dict, changes: dict) -> dict:
    return {
        "id": before["id"],
        "label": label,
        "changes": changes,
        "fingerprint": fingerprint(before),
    }


def _inherited_changes(album) -> tuple[dict, set]:
    """What `Album.store` would copy down to the Album's Items, and delete from them."""
    updates, deletes = {}, set()
    for key in album._dirty:
        if key in Album.item_keys:
            updates[key] = album[key]
        elif key in album._fields:
            continue
        elif key not in album:
            deletes.add(key)
        else:
            updates[key] = album[key]
    return updates, deletes


def _item_entry(lib, item, label: str, before: dict, move: bool) -> dict | None:
    changes = _diff(before, to_json(item))
    if move and lib.directory in util.ancestry(item.path):
        destination = item.destination()
        if destination != item.path:
            changes["path"] = {"old": json_value(item.path), "new": json_value(destination)}
    return _entry(label, before, changes) if changes else None


def _build_preview(lib, album: bool, query: str, mods, dels, move: bool) -> dict:
    """Work out the changes on in-memory objects, which are never stored.

    Limitation: `%aunique` reads the database directly, so previewed paths don't
    reflect pending values it uses. Confirm always moves files to their real
    destination and reports where they ended up.
    """
    forget_cached_paths(lib)
    preview = {"albums": [], "items": []}
    if album:
        for obj in lib.albums(query):
            items = [(i, format(i), to_json(i)) for i in obj.items()]
            label, before = format(obj), to_json(obj)
            _modify_object(obj, Album, mods, dels)
            if changes := _diff(before, to_json(obj)):
                preview["albums"].append(_entry(label, before, changes))
            updates, deletes = _inherited_changes(obj)
            # Not dirty, so `Item._cached_album` won't reload it from the database.
            obj.clear_dirty()
            for item, item_label, item_before in items:
                item.update(updates)
                for key in deletes & set(item.keys()):
                    del item[key]
                item._cached_album = obj
                entry = _item_entry(lib, item, item_label, item_before, move)
                if entry:
                    preview["items"].append(entry)
    else:
        for item in lib.items(query):
            label, before = format(item), to_json(item)
            _modify_object(item, Item, mods, dels)
            entry = _item_entry(lib, item, label, before, move)
            if entry:
                preview["items"].append(entry)
    return preview


def _set_fields(obj, changes: dict) -> None:
    for key, change in changes.items():
        if key == "path":
            continue
        if change["new"] is None:
            del obj[key]
        else:
            obj[key] = change["new"]


def _apply_item(lib, entry: dict, write: bool, move: bool) -> str:
    """Apply one Item's changes and return where its file ended up."""
    item = lib.get_item(entry["id"])
    _set_fields(item, entry["changes"])
    tags_changed = entry["changes"].keys() != {"path"}
    write_failed = write and tags_changed and not item.try_write()
    # Not only when the Preview showed a new path: see `_build_preview`.
    if move and lib.directory in util.ancestry(item.path):
        item.move()
    item.store()
    if write_failed:
        raise RuntimeError("database updated, but writing tags to the file failed")
    return json_value(item.path)


def _apply_album(lib, entry: dict) -> None:
    album = lib.get_album(entry["id"])
    _set_fields(album, entry["changes"])
    # The Preview already holds each Item's changes, so don't copy them down twice.
    album.store(inherit=False)


def _summary(op: dict) -> dict:
    """A Modification without its Preview, which can be large."""
    result = op["result"]
    counts = {
        "albums": len(result["preview"]["albums"]),
        "items": len(result["preview"]["items"]),
        "applied": len(result.get("applied", [])),
        "failed": len(result.get("failed", [])),
    }
    keys = ("id", "status", "request", "created_at", "updated_at")
    return {k: op[k] for k in keys} | {"counts": counts}


class Modifications:
    def __init__(self, lib, store: Store):
        self.lib = lib
        self.store = store
        # One at a time: queued Modifications wait for the one in progress.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="modify")

    def create(self, request: dict) -> dict:
        album, query = request["scope"] == "album", request["query"]
        mods, dels = _parse_changes(album, request["set"], request["delete"])
        write, move = ui.should_write(), ui.should_move()
        preview = _build_preview(self.lib, album, query, mods, dels, move)
        status = "awaiting_confirmation" if any(preview.values()) else "no_changes"
        result = {"write": write, "move": move, "preview": preview}
        return self.get(self.store.create(KIND, status, request, result))

    def get(self, id_: int) -> dict:
        op = self.store.get(id_, KIND)
        if op is None:
            raise HTTPException(404, "modification not found")
        return op

    def page(self, statuses: list[str], limit: int, offset: int) -> dict:
        total, ops = self.store.page(KIND, statuses, limit, offset)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [_summary(op) for op in ops],
        }

    def _stale(self, preview: dict) -> list[dict]:
        stale = []
        for kind, getter in (("albums", self.lib.get_album), ("items", self.lib.get_item)):
            for entry in preview[kind]:
                obj = getter(entry["id"])
                if obj is None or fingerprint(to_json(obj)) != entry["fingerprint"]:
                    stale.append({"kind": kind, "id": entry["id"]})
        return stale

    def _reject(self, op: dict, stale: list[dict]) -> None:
        self.store.update(op["id"], "rejected", op["result"] | {"stale": stale})

    def confirm(self, id_: int) -> dict:
        op = self.get(id_)
        if op["status"] != "awaiting_confirmation":
            raise HTTPException(409, f"cannot confirm while {op['status']}")
        if stale := self._stale(op["result"]["preview"]):
            self._reject(op, stale)
            raise HTTPException(409, {"detail": "library changed since Preview", "stale": stale})
        if not self.store.transition(id_, "awaiting_confirmation", "queued"):
            raise HTTPException(409, "already confirmed")
        self.executor.submit(self._apply, id_)
        return self.get(id_)

    def _apply_one(self, id_: int, result: dict, kind: str, entry: dict, apply_entry) -> None:
        ref = {"kind": kind, "id": entry["id"]}
        result["current"] = ref
        self.store.update(id_, "applying", result)
        try:
            path = apply_entry(entry)
            result["applied"].append(ref | ({"path": path} if path else {}))
        except Exception as exc:
            log.exception("failed to apply %s", ref)
            result["failed"].append(ref | {"error": str(exc)})

    def _apply(self, id_: int) -> None:
        with self.lib.music_dir_context():
            self._apply_all(id_)

    def _apply_all(self, id_: int) -> None:
        op = self.get(id_)
        result = op["result"] | {"applied": [], "failed": [], "current": None}
        try:
            # An earlier queued Modification may have changed these objects.
            if stale := self._stale(result["preview"]):
                self._reject(op, stale)
                return
            self.store.update(id_, "applying", result)
            write, move = result["write"], result["move"]
            for entry in result["preview"]["albums"]:
                self._apply_one(id_, result, "albums", entry, lambda e: _apply_album(self.lib, e))
            # Items' new paths depend on the Albums stored above.
            forget_cached_paths(self.lib)
            for entry in result["preview"]["items"]:
                self._apply_one(id_, result, "items", entry, lambda e: _apply_item(self.lib, e, write, move))
            result["current"] = None
            self.store.update(id_, "partial" if result["failed"] else "applied", result)
        except Exception as exc:
            log.exception("modification %s failed", id_)
            self.store.update(id_, "partial", result | {"error": str(exc)})
