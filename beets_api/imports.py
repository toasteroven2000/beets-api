"""The beets importer, with its prompts answered over HTTP.

A beets import runs in its own thread. When beets asks for a Choice, the thread
publishes a prompt and waits until a request answers it.
"""

import logging
import os
import queue
import threading
import uuid

from beets import config
from beets.autotag import tag_album, tag_item
from beets.importer import Action, DuplicateAction, ImportAbortError, ImportSession
from fastapi import HTTPException

from .import_paths import inside
from .library import forget_cached_paths, json_value, to_json
from .store import Store

log = logging.getLogger(__name__)

KIND = "import"
ACTIONS = {
    "asis": Action.ASIS,
    "skip": Action.SKIP,
    "tracks": Action.TRACKS,
    "albums": Action.ALBUMS,
}
DUPLICATE_ACTIONS = {
    "skip": DuplicateAction.SKIP,
    "keep": DuplicateAction.KEEP,
    "remove": DuplicateAction.REMOVE,
    "merge": DuplicateAction.MERGE,
}


def _paths(paths) -> list[str]:
    return [json_value(p) for p in paths]


def _track_changes(item, data: dict) -> dict:
    changes = {
        key: {"old": json_value(item.get(key)), "new": json_value(value)}
        for key, value in data.items()
        if item.get(key) != value
    }
    return {"path": json_value(item.path), "changes": changes}


def _describe_album_match(index: int, match) -> dict:
    info = match.info
    return {
        "index": index,
        "distance": float(match.distance),
        "artist": info.get("artist"),
        "album": info.get("album"),
        "year": info.get("year"),
        "id": info.get("album_id"),
        "data_source": info.get("data_source"),
        "tracks": [_track_changes(item, data) for item, data in match.merged_pairs],
        "extra_items": [json_value(i.path) for i in match.extra_items],
        "extra_tracks": [t.get("title") for t in match.extra_tracks],
    }


def _describe_track_match(index: int, match) -> dict:
    info = match.info
    return {
        "index": index,
        "distance": float(match.distance),
        "artist": info.get("artist"),
        "title": info.get("title"),
        "id": info.get("track_id"),
        "data_source": info.get("data_source"),
        "track": _track_changes(match.item, dict(info.item_data)),
    }


def _summary(op: dict) -> dict:
    """An Import without its prompt, whose Candidates can be large."""
    result = op["result"]
    outcomes = result.get("outcomes", [])
    counts = {
        "choices": len(result["choices"]),
        "tasks": len(outcomes),
        "skipped": sum(o["skipped"] for o in outcomes),
        "items": sum(len(o["items"]) for o in outcomes),
    }
    keys = ("id", "status", "request", "created_at", "updated_at")
    return {k: op[k] for k in keys} | {"counts": counts}


class ApiImportSession(ImportSession):
    def __init__(
        self, lib, paths: list[str], op_id: int, store: Store, client: str | None = None
    ):
        super().__init__(lib, None, [os.fsencode(p) for p in paths], None)
        self.op_id = op_id
        self.store = store
        self.client = client
        self.lock = threading.Lock()
        self.answers: queue.Queue = queue.Queue()
        self.prompt: dict | None = None
        self.abort_requested = False
        self.history: list[dict] = []
        self.tasks: list = []

    def _save(self, status: str, **extra) -> None:
        result = {"prompt": self.prompt, "choices": self.history} | extra
        self.store.update(self.op_id, status, result)

    def _ask(self, task, kind: str, choices: list[str], **details) -> dict:
        with self.lock:
            if self.abort_requested:
                raise ImportAbortError()
            self.prompt = {
                "id": str(uuid.uuid4()),
                "kind": kind,
                "paths": _paths(task.paths),
                "choices": [*choices, "abort"],
                **details,
            }
            self._save("waiting_for_choice")
        answer = self.answers.get()
        self.history.append({"paths": _paths(task.paths), "kind": kind, **answer})
        self._save("running")
        if answer["action"] == "abort":
            raise ImportAbortError()
        return answer

    def answer(self, request: dict) -> None:
        with self.lock:
            prompt = self.prompt
            if prompt is None or prompt["id"] != request["prompt_id"]:
                raise HTTPException(409, "this prompt is no longer current")
            action = request["action"]
            if action not in prompt["choices"]:
                raise HTTPException(422, f"choose one of {prompt['choices']}")
            if action == "apply" and not (
                request["candidate"] is not None
                and 0 <= request["candidate"] < len(prompt["candidates"])
            ):
                raise HTTPException(422, "apply needs a valid candidate index")
            if action == "search" and not (
                request["search_ids"] or (request["artist"] and request["name"])
            ):
                raise HTTPException(422, "search needs search_ids, or artist and name")
            self.prompt = None
            self.answers.put(request)

    def abort(self) -> None:
        with self.lock:
            self.abort_requested = True
            if self.prompt is not None:
                self.prompt = None
                self.answers.put({"prompt_id": None, "action": "abort"})

    def _choices(self, candidates, base: list[str]) -> list[str]:
        return (["apply"] if candidates else []) + base

    def _track(self, task) -> None:
        """Audit the task once beets has finished it, files and all."""
        if task in self.tasks:
            return
        self.tasks.append(task)
        finalize = task.finalize

        def finalize_and_audit(session):
            finalize(session)
            self._audit(task)

        task.finalize = finalize_and_audit

    def _audit(self, task) -> None:
        if task.skip:
            return
        match = (
            {"data_source": task.match.info.get("data_source"),
             "id": task.match.info.get("album_id" if task.is_album else "track_id")}
            if task.match and task.choice_flag is Action.APPLY else "asis"
        )
        changes = {"source": _paths(task.paths), "match": match}
        if task.is_album:
            target = f"album:{task.album.id}"
            changes["items"] = [to_json(i, ["id", "path"]) for i in task.imported_items()]
        else:
            target = f"item:{task.item.id}"
            changes["path"] = json_value(task.item.path)
        model = target.split(":")[0]
        self.store.audit(f"{model}.import", target, changes, self.client, self.op_id)
        # Only the "remove" Choice deletes the duplicates from the Library.
        if getattr(task, "api_removed_duplicates", None):
            for dup in task.api_removed_duplicates:
                self.store.audit(
                    f"{dup['model']}.remove", f"{dup['model']}:{dup['id']}",
                    {"removed": dup["fields"], "replaced_by": target},
                    self.client, self.op_id,
                )

    def choose_match(self, task):
        self._track(task)
        while True:
            answer = self._ask(
                task,
                "album",
                self._choices(task.candidates, ["asis", "skip", "tracks", "albums", "search"]),
                current={"artist": task.cur_artist, "album": task.cur_album},
                recommendation=task.rec.name,
                candidates=[_describe_album_match(i, m) for i, m in enumerate(task.candidates)],
            )
            action = answer["action"]
            if action == "apply":
                return task.candidates[answer["candidate"]]
            if action != "search":
                return ACTIONS[action]
            _, _, proposal = tag_album(
                task.items, answer["artist"], answer["name"], answer["search_ids"]
            )
            task.candidates, task.rec = proposal

    def choose_item(self, task):
        self._track(task)
        while True:
            answer = self._ask(
                task,
                "item",
                self._choices(task.candidates, ["asis", "skip", "search"]),
                current={"artist": task.item.artist, "title": task.item.title},
                recommendation=task.rec.name,
                candidates=[_describe_track_match(i, m) for i, m in enumerate(task.candidates)],
            )
            action = answer["action"]
            if action == "apply":
                return task.candidates[answer["candidate"]]
            if action != "search":
                return ACTIONS[action]
            task.candidates, task.rec = tag_item(
                task.item, answer["artist"], answer["name"], answer["search_ids"]
            )

    def get_duplicate_action(self, task, found_duplicates):
        fields = ["id", "albumartist", "album"] if task.is_album else ["id", "artist", "title"]
        choices = ["skip", "keep", "remove", *(["merge"] if task.is_album else [])]
        answer = self._ask(
            task,
            "duplicate",
            choices,
            existing=[to_json(d, fields) for d in found_duplicates],
        )
        if answer["action"] == "remove":
            model = "album" if task.is_album else "item"
            task.api_removed_duplicates = [
                {"model": model, "id": d.id, "fields": to_json(d)} for d in found_duplicates
            ]
        return DUPLICATE_ACTIONS[answer["action"]]

    def _outcomes(self) -> list[dict]:
        outcomes = []
        for task in self.tasks:
            items = [] if task.skip else task.imported_items()
            outcomes.append({
                "paths": _paths(task.paths),
                "skipped": task.skip,
                "items": [to_json(i, ["id", "path"]) for i in items if i.id],
            })
        return outcomes

    def execute(self) -> None:
        try:
            self.run()
            status = "aborted" if self.abort_requested else "completed"
            extra = {}
        except Exception as exc:
            log.exception("import %s failed", self.op_id)
            status, extra = "failed", {"error": str(exc)}
        # An abort Choice ends the run the same way as an abort request.
        if any(c["action"] == "abort" for c in self.history):
            status = "aborted"
        self.prompt = None
        self._save(status, outcomes=self._outcomes(), **extra)


class Imports:
    def __init__(self, lib, store: Store, roots: list[str] | None = None):
        self.lib = lib
        self.store = store
        self.roots = roots
        self.lock = threading.Lock()
        self.sessions: dict[str, ApiImportSession] = {}
        self.active: ApiImportSession | None = None

    def start(self, request: dict, client: str | None = None) -> dict:
        paths = request["paths"]
        if not paths:
            raise HTTPException(422, "no paths given")
        if missing := [p for p in paths if not os.path.exists(p)]:
            raise HTTPException(422, f"paths do not exist: {missing}")
        if self.roots is not None and (
            outside := [p for p in paths if not inside(p, self.roots)]
        ):
            raise HTTPException(403, f"paths are outside BEETS_API_IMPORT_PATHS: {outside}")
        with self.lock:
            if self.active is not None:
                raise HTTPException(409, f"import {self.active.op_id} is still active")
            # Beets' import config is global, which is why one Import runs at a time.
            settings = config["import"]
            settings["autotag"] = True  # Without autotag there are no prompts.
            settings["resume"] = True  # Finished Import Tasks are skipped if re-imported.
            settings["quiet"] = False
            settings["pretend"] = False
            settings["singletons"] = request["singletons"]
            forget_cached_paths(self.lib)
            id_ = self.store.create(KIND, "running", request, {"prompt": None, "choices": []})
            session = ApiImportSession(self.lib, paths, id_, self.store, client)
            self.sessions[id_] = session
            self.active = session
        threading.Thread(target=self._run, args=(session,), daemon=True).start()
        return self.get(id_)

    def _run(self, session: ApiImportSession) -> None:
        try:
            with self.lib.music_dir_context():
                session.execute()
        finally:
            with self.lock:
                self.active = None

    def get(self, id_: int) -> dict:
        op = self.store.get(id_, KIND)
        if op is None:
            raise HTTPException(404, "import not found")
        return op

    def page(self, statuses: list[str], limit: int, offset: int) -> dict:
        total, ops = self.store.page(KIND, statuses, limit, offset)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "results": [_summary(op) for op in ops],
        }

    def _active_session(self, id_: int) -> ApiImportSession:
        self.get(id_)
        with self.lock:
            active = self.active
        if active is None or active.op_id != id_:
            raise HTTPException(409, "this import is not active")
        return active

    def choose(self, id_: int, request: dict) -> dict:
        self._active_session(id_).answer(request)
        return self.get(id_)

    def abort(self, id_: int) -> dict:
        self._active_session(id_).abort()
        return self.get(id_)
