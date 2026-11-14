"""Undoing an Import: remove what it added to the Library, and the files it copied in.

beets has no undo, so this works from the Audit, which records every Album and
Item an Import made and where its files went. It only ever acts when that is safe:

- the Import copied its files, leaving the originals where they were (the mode
  is recorded when the Import starts, as beets' config can change later);
- nothing it made has been changed since (any later Audit entry for it, or a
  file that is no longer where the Audit says);
- it did not replace existing Albums or Items, whose files are already gone.

Only paths the Audit names are deleted, and only inside the music directory.
"""

import os
from datetime import UTC, datetime

from beets import util
from fastapi import HTTPException

from .caller import Caller
from .library import json_value, to_json
from .roots import Roots

COPY = "copy"
MODE_REASONS = {
    "move": "the Import moved its files, so the originals are gone",
    "copy_delete": "the Import deleted the originals after copying them",
    "link": "the Import linked its files; only a plain copy can be undone",
    "in_place": "the Import added its files where they were, so undoing would delete the originals",
    None: "this Import did not record whether it copied its files (it ran before that was kept)",
}
IMPORT_ACTIONS = ("album.import", "item.import")


def import_mode(settings) -> str:
    """What beets' import config does with the files: move, copy, copy_delete, link or in_place."""
    if settings["move"].get(bool):
        return "move"
    if settings["copy"].get(bool):
        return "copy_delete" if settings["delete"].get(bool) else COPY
    if any(settings[kind].get(bool) for kind in ("link", "hardlink", "reflink")):
        return "link"
    return "in_place"


def _is_import_of(entry: dict, id_: int) -> bool:
    return entry["action"] in IMPORT_ACTIONS and entry["operation_id"] == id_


def _created(entries: list[dict], lib, reasons: list[str]) -> tuple[list[dict], list[dict]]:
    """The Albums and Items the Import added that are still in the Library."""
    albums, items = [], []
    for entry in entries:
        model, _, raw = entry["target"].partition(":")
        id_ = int(raw)
        if model == "album":
            if (album := lib.get_album(id_)) is None:
                continue  # removed since; there is nothing left to undo
            recorded = {i["id"]: i["path"] for i in entry["changes"]["items"]}
            present = {i.id: json_value(i.path) for i in album.items()}
            if moved := [p for i, p in recorded.items() if i in present and present[i] != p]:
                reasons.append(f"files of {entry['target']} are no longer where the Import put them: {moved}")
            if extra := sorted(set(present) - set(recorded)):
                reasons.append(f"{entry['target']} now holds items the Import did not add: {extra}")
            albums.append({
                "target": entry["target"], "id": id_, "albumartist": album.albumartist,
                "album": album.album,
                "items": [{"id": i, "path": p} for i, p in present.items() if i in recorded],
                "art": json_value(album.artpath) if album.artpath else None,
            })
        else:
            if (item := lib.get_item(id_)) is None:
                continue
            if json_value(item.path) != entry["changes"]["path"]:
                reasons.append(f"the file of {entry['target']} is no longer where the Import put it")
            items.append({"target": entry["target"], "id": id_, "artist": item.artist,
                          "title": item.title, "path": json_value(item.path)})
    return albums, items


def plan_undo(lib, store, op: dict, active: bool) -> dict:
    """What undoing this Import would do, and why it may not. Changes nothing."""
    id_ = op["id"]
    reasons = []
    if active or op["status"] in ("running", "waiting_for_choice"):
        reasons.append("the Import is still active")
    mode = op["result"].get("mode")
    if mode != COPY:
        reasons.append(MODE_REASONS[mode if mode in MODE_REASONS else None])
    if op["result"].get("undone"):
        reasons.append("this Import was already undone")

    entries = store.audit_entries(operation_id=id_)
    made = [e for e in entries if e["action"] in IMPORT_ACTIONS]
    replaced = [e for e in entries if e["action"].endswith(".remove") and "undo_of_import" not in e["changes"]]
    if replaced:
        reasons.append(
            f"the Import replaced {len(replaced)} existing "
            f"{'record' if len(replaced) == 1 else 'records'} whose files were deleted: "
            + ", ".join(e["target"] for e in replaced)
        )
    changed_since = [
        {k: e[k] for k in ("id", "at", "action", "target")}
        for e in store.audit_entries(targets=[e["target"] for e in made])
        if not _is_import_of(e, id_)
    ]
    if changed_since:
        reasons.append(f"{len(changed_since)} later changes were made to what the Import added")

    albums, items = _created(made, lib, reasons)
    music_dir = Roots([lib.directory])
    files = [p for a in albums for p in (*(i["path"] for i in a["items"]), a["art"]) if p]
    files += [i["path"] for i in items]
    if outside := [p for p in files if not music_dir.contains(p)]:
        reasons.append(f"files are outside the music directory: {outside}")
    return {
        "import_id": id_, "mode": mode, "allowed": not reasons, "reasons": reasons,
        "albums": albums, "items": items, "changes_since": changed_since,
    }


def _delete(lib, path: str | None) -> bool:
    if not path or not os.path.isfile(path):
        return False
    os.remove(path)
    util.prune_dirs(os.path.dirname(os.fsencode(path)), root=lib.directory)
    return True


def undo_import(lib, store, op: dict, active: bool, caller: Caller) -> dict:
    """Remove what the Import added, then its files, auditing each as it goes."""
    plan = plan_undo(lib, store, op, active)
    if not plan["allowed"]:
        raise HTTPException(409, "cannot undo this Import: " + "; ".join(plan["reasons"]))
    removed = []
    for album in plan["albums"]:
        record = lib.get_album(album["id"])
        fields = {"album": to_json(record), "items": [to_json(i) for i in record.items()]}
        record.remove(delete=False, with_items=True)  # the files are deleted below, from the Audit's paths
        files = [p for p in (*(i["path"] for i in album["items"]), album["art"]) if _delete(lib, p)]
        removed.append(store.audit(
            "album.remove", album["target"],
            {"removed": fields, "files": files, "undo_of_import": op["id"]},
            caller, op["id"],
        ))
    for item in plan["items"]:
        record = lib.get_item(item["id"])
        fields = to_json(record)
        record.remove(delete=False, with_album=False)
        files = [item["path"]] if _delete(lib, item["path"]) else []
        removed.append(store.audit(
            "item.remove", item["target"],
            {"removed": fields, "files": files, "undo_of_import": op["id"]},
            caller, op["id"],
        ))
    store.update(op["id"], op["status"], op["result"] | {"undone": {"at": datetime.now(UTC).isoformat()}})
    return plan | {"allowed": False, "undone": True, "audit": removed}
