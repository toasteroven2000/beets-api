"""Purge: delete Trash folders for good, with a Preview first like a Removal.

A Purge is the only step that can't be undone, so it is the only one that
deletes files. It is recorded as an operation and, for each folder, an Audit entry.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from fastapi import HTTPException

from .caller import Caller
from .changes import Purged
from .store import Store
from .trash import Trash

log = logging.getLogger(__name__)

KIND = "purge"


def _time(value: str) -> datetime:
    when = datetime.fromisoformat(value)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _summary(op: dict) -> dict:
    """A Purge without its Preview."""
    result = op["result"]
    counts = {
        "folders": len(result.get("preview", {"folders": []})["folders"]),
        "purged": len(result.get("purged", [])),
        "failed": len(result.get("failed", [])),
    }
    keys = ("id", "status", "request", "created_at", "updated_at")
    return {k: op[k] for k in keys} | {"counts": counts}


class Purges:
    def __init__(self, store: Store, trash: Trash):
        self.store = store
        self.trash = trash
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="purge")

    def listing(self) -> dict:
        """Each Trash folder, with what the Audit knows about the operation that made it."""
        folders = []
        for folder in self.trash.folders():
            entries = self.store.audit_entries(operation_id=int(folder["name"])) \
                if folder["name"].isdigit() else []
            folders.append(folder | {
                "operation_id": int(folder["name"]) if folder["name"].isdigit() else None,
                "actions": sorted({e["action"] for e in entries}),
                "actors": sorted({e["actor"] for e in entries if e["actor"]}),
            })
        return {"configured": self.trash.configured, "folders": folders}

    def create(self, request: dict) -> dict:
        """Start building the Preview in the background; poll until it is no longer `previewing`."""
        if (names := request["folders"]) is not None:
            if unknown := sorted(set(names) - set(self.trash.names())):
                raise HTTPException(422, f"not Trash folders: {unknown}")
        result = {"trash_configured": self.trash.configured}
        id_ = self.store.create(KIND, "previewing", request, result)
        self.executor.submit(self._preview, id_, request, result)
        return self.get(id_)

    def _preview(self, id_: int, request: dict, result: dict) -> None:
        try:
            folders = self.trash.folders()  # walks every file, so it is not done in the request
            if (names := request["folders"]) is not None:
                folders = [f for f in folders if f["name"] in names]
            if request["older_than"] is not None:
                before = _time(request["older_than"])
                folders = [f for f in folders if _time(f["modified"]) < before]
        except Exception as exc:
            log.exception("preview of purge %s failed", id_)
            self.store.update(id_, "failed", result | {"error": str(exc)})
            return
        status = "awaiting_confirmation" if folders else "no_changes"
        self.store.update(id_, status, result | {"preview": {"folders": folders}})

    def get(self, id_: int) -> dict:
        op = self.store.get(id_, KIND)
        if op is None:
            raise HTTPException(404, "purge not found")
        return op

    def page(self, statuses: list[str], limit: int, offset: int) -> dict:
        total, ops = self.store.page(KIND, statuses, limit, offset)
        return {"total": total, "limit": limit, "offset": offset,
                "results": [_summary(op) for op in ops]}

    def _stale(self, preview: dict) -> list[str]:
        now = {f["name"]: f["fingerprint"] for f in self.trash.folders()}
        return [f["name"] for f in preview["folders"] if now.get(f["name"]) != f["fingerprint"]]

    def confirm(self, id_: int, caller: Caller = Caller()) -> dict:
        op = self.get(id_)
        if op["status"] != "awaiting_confirmation":
            raise HTTPException(409, f"cannot confirm while {op['status']}")
        self.trash.require()
        # Whether the Trash changed since the Preview is checked when it is applied.
        if not self.store.transition(id_, "awaiting_confirmation", "queued"):
            raise HTTPException(409, "already confirmed")
        self.executor.submit(self._apply, id_, caller)
        return self.get(id_)

    def _apply(self, id_: int, caller: Caller) -> None:
        op = self.get(id_)
        result = op["result"] | {"purged": [], "failed": []}
        try:
            if stale := self._stale(result["preview"]):
                self.store.update(id_, "rejected", result | {"stale": stale})
                return
            self.store.update(id_, "applying", result)
            for folder in result["preview"]["folders"]:
                try:
                    self.trash.purge(folder["name"])
                except Exception as exc:
                    log.exception("failed to purge %s", folder["name"])
                    result["failed"].append({"name": folder["name"], "error": str(exc)})
                    continue
                result["purged"].append(folder["name"])
                self.store.audit(
                    "trash.purge", f"trash:{folder['name']}",
                    Purged(folder["files"], folder["bytes"]), caller, id_,
                )
            self.store.update(id_, "partial" if result["failed"] else "applied", result)
        except Exception as exc:
            log.exception("purge %s failed", id_)
            self.store.update(id_, "partial", result | {"error": str(exc)})
