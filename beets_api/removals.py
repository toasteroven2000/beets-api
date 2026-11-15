"""Remove with a Preview first: `beet remove` split across two HTTP requests.

The music files go to the Trash, but only those inside the music directory.
Anything outside it is left where it is and reported.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from .caller import Caller
from .changes import Removed
from .library import check_query, fingerprint, json_value, to_json
from .store import Progress, Store
from .trash import Trash

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
    preview = result.get("preview", {"albums": [], "items": []})  # none until it is built
    counts = {
        "albums": len(preview["albums"]),
        "items": len(preview["items"]) + sum(len(a["items"]) for a in preview["albums"]),
        "removed": len(result.get("removed", [])),
        "failed": len(result.get("failed", [])),
    }
    keys = ("id", "status", "request", "created_at", "updated_at")
    return {k: op[k] for k in keys} | {"counts": counts}


class Removals:
    def __init__(self, lib, store: Store, trash: Trash):
        self.lib = lib
        self.store = store
        self.trash = trash
        # One at a time, like Modifications.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remove")

    def create(self, request: dict) -> dict:
        if not request["query"].strip():
            raise HTTPException(422, "a query is required: an empty one matches the whole Library")
        check_query(request["query"], request["scope"] == "album")
        result = {"trash_configured": self.trash.configured}
        id_ = self.store.create(KIND, "previewing", request, result)
        self.executor.submit(self._preview, id_, request, result)
        return self.get(id_)

    def _preview(self, id_: int, request: dict, result: dict) -> None:
        """Build the Preview in the background; the request has already returned."""
        try:
            with self.lib.music_dir_context():
                preview = _build_preview(self.lib, request["scope"] == "album", request["query"])
        except Exception as exc:
            log.exception("preview of removal %s failed", id_)
            self.store.update(id_, "failed", result | {"error": str(exc)})
            return
        status = "awaiting_confirmation" if any(preview.values()) else "no_changes"
        self.store.update(id_, status, result | {"preview": preview})

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
        self.trash.require()
        # Staleness is checked when it is applied, so confirming returns at once.
        if not self.store.transition(id_, "awaiting_confirmation", "queued"):
            raise HTTPException(409, "already confirmed")
        self.executor.submit(self._apply, id_, caller)
        return self.get(id_)

    def _remove_album(self, entry: dict, id_: int, caller: Caller) -> dict:
        album = self.lib.get_album(entry["id"])
        snapshot = {"album": to_json(album), "items": [to_json(i) for i in album.items()]}
        album.remove(delete=False, with_items=True)
        files, left = self.trash.put_all(str(id_), [*(i["path"] for i in entry["items"]), entry["art"]])
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
        files, left = self.trash.put_all(str(id_), [entry["path"]])
        self.store.audit(
            "item.remove", f"item:{entry['id']}", Removed(snapshot, files, left), caller, id_
        )
        if album and self.lib.get_album(album.id) is None:
            files, left = self.trash.put_all(str(id_), [art])
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
            progress = Progress(self.store, id_)
            progress.save("applying", result, force=True)
            for kind, remove in (("albums", self._remove_album), ("items", self._remove_item)):
                for entry in result["preview"][kind]:
                    ref = {"kind": kind, "id": entry["id"]}
                    result["current"] = ref
                    progress.save("applying", result)
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
