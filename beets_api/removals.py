"""Remove with a Preview first: `beet remove -d` split across two HTTP requests.

The music files are always deleted, but only inside the music directory.
Anything outside it is left where it is and reported.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from .caller import Caller
from .changes import Removed
from .library import delete_file, fingerprint, json_value, to_json
from .roots import Roots
from .store import Store

log = logging.getLogger(__name__)

KIND = "removal"


def _item_entry(item) -> dict:
    fields = to_json(item)
    return {"id": item.id, "path": json_value(item.path), "fingerprint": fingerprint(fields)}


def _build_preview(lib, album: bool, query: str) -> dict:
    """What would be removed. Nothing is changed."""
    preview = {"albums": [], "items": []}
    if album:
        for obj in lib.albums(query):
            preview["albums"].append({
                "id": obj.id,
                "label": format(obj),
                "fingerprint": fingerprint(to_json(obj)),
                "items": [_item_entry(i) for i in obj.items()],
                "art": json_value(obj.artpath) if obj.artpath else None,
            })
    else:
        for item in lib.items(query):
            preview["items"].append(
                {"label": format(item), "album_id": item.album_id} | _item_entry(item)
            )
    return preview


def _summary(op: dict) -> dict:
    """A Removal without its Preview, which can be large."""
    result = op["result"]
    counts = {
        "albums": len(result["preview"]["albums"]),
        "items": len(result["preview"]["items"])
        + sum(len(a["items"]) for a in result["preview"]["albums"]),
        "removed": len(result.get("removed", [])),
        "failed": len(result.get("failed", [])),
    }
    keys = ("id", "status", "request", "created_at", "updated_at")
    return {k: op[k] for k in keys} | {"counts": counts}


class Removals:
    def __init__(self, lib, store: Store):
        self.lib = lib
        self.store = store
        self.music_dir = Roots([lib.directory])
        # One at a time, like Modifications.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remove")

    def create(self, request: dict) -> dict:
        if not request["query"].strip():
            raise HTTPException(422, "a query is required: an empty one matches the whole Library")
        preview = _build_preview(self.lib, request["scope"] == "album", request["query"])
        status = "awaiting_confirmation" if any(preview.values()) else "no_changes"
        return self.get(self.store.create(KIND, status, request, {"preview": preview}))

    def get(self, id_: int) -> dict:
        op = self.store.get(id_, KIND)
        if op is None:
            raise HTTPException(404, "removal not found")
        return op

    def page(self, statuses: list[str], limit: int, offset: int) -> dict:
        total, ops = self.store.page(KIND, statuses, limit, offset)
        return {"total": total, "limit": limit, "offset": offset,
                "results": [_summary(op) for op in ops]}

    def _stale(self, preview: dict) -> list[dict]:
        stale = []
        for entry in preview["albums"]:
            album = self.lib.get_album(entry["id"])
            if (
                album is None
                or fingerprint(to_json(album)) != entry["fingerprint"]
                or [_item_entry(i) for i in album.items()] != entry["items"]
            ):
                stale.append({"kind": "albums", "id": entry["id"]})
        for entry in preview["items"]:
            item = self.lib.get_item(entry["id"])
            if item is None or _item_entry(item)["fingerprint"] != entry["fingerprint"]:
                stale.append({"kind": "items", "id": entry["id"]})
        return stale

    def _reject(self, op: dict, stale: list[dict]) -> None:
        self.store.update(op["id"], "rejected", op["result"] | {"stale": stale})

    def confirm(self, id_: int, caller: Caller = Caller()) -> dict:
        op = self.get(id_)
        if op["status"] != "awaiting_confirmation":
            raise HTTPException(409, f"cannot confirm while {op['status']}")
        if stale := self._stale(op["result"]["preview"]):
            self._reject(op, stale)
            raise HTTPException(409, {"detail": "library changed since Preview", "stale": stale})
        if not self.store.transition(id_, "awaiting_confirmation", "queued"):
            raise HTTPException(409, "already confirmed")
        self.executor.submit(self._apply, id_, caller)
        return self.get(id_)

    def _delete_files(self, paths: list[str | None]) -> tuple[list[str], list[str]]:
        """Delete the files inside the music directory; return (deleted, left_in_place)."""
        deleted, left = [], []
        for path in filter(None, paths):
            if not self.music_dir.contains(path):
                left.append(path)
            elif delete_file(self.lib, path):
                deleted.append(path)
        return deleted, left

    def _remove_album(self, entry: dict, id_: int, caller: Caller) -> dict:
        album = self.lib.get_album(entry["id"])
        snapshot = {"album": to_json(album), "items": [to_json(i) for i in album.items()]}
        album.remove(delete=False, with_items=True)
        files, left = self._delete_files([*(i["path"] for i in entry["items"]), entry["art"]])
        return self.store.audit(
            "album.remove", f"album:{entry['id']}", Removed(snapshot, files, left), caller, id_
        )

    def _remove_item(self, entry: dict, id_: int, caller: Caller) -> None:
        item = self.lib.get_item(entry["id"])
        album = self.lib.get_album(item.album_id) if item.album_id is not None else None
        album_snapshot = {"album": to_json(album), "items": []} if album else None
        art = json_value(album.artpath) if album and album.artpath else None
        snapshot = to_json(item)
        item.remove(delete=False, with_album=True)  # beets drops an Album left empty
        files, left = self._delete_files([entry["path"]])
        self.store.audit(
            "item.remove", f"item:{entry['id']}", Removed(snapshot, files, left), caller, id_
        )
        if album and self.lib.get_album(album.id) is None:
            files, left = self._delete_files([art])
            self.store.audit(
                "album.remove", f"album:{album.id}", Removed(album_snapshot, files, left), caller, id_
            )

    def _apply(self, id_: int, caller: Caller) -> None:
        with self.lib.music_dir_context():
            self._apply_all(id_, caller)

    def _apply_all(self, id_: int, caller: Caller) -> None:
        op = self.get(id_)
        result = op["result"] | {"removed": [], "failed": [], "current": None}
        try:
            # An earlier queued Removal or Modification may have changed these.
            if stale := self._stale(result["preview"]):
                self._reject(op, stale)
                return
            self.store.update(id_, "applying", result)
            for kind, remove in (("albums", self._remove_album), ("items", self._remove_item)):
                for entry in result["preview"][kind]:
                    ref = {"kind": kind, "id": entry["id"]}
                    result["current"] = ref
                    self.store.update(id_, "applying", result)
                    try:
                        remove(entry, id_, caller)
                    except Exception as exc:
                        log.exception("failed to remove %s", ref)
                        result["failed"].append(ref | {"error": str(exc)})
                    else:
                        result["removed"].append(ref)
            result["current"] = None
            self.store.update(id_, "partial" if result["failed"] else "applied", result)
        except Exception as exc:
            log.exception("removal %s failed", id_)
            self.store.update(id_, "partial", result | {"error": str(exc)})
