"""HTTP routes. Run with:

python -m uvicorn --factory beets_api.app:create_app --host 127.0.0.1 --port 8338
"""

import json
import logging
import os
from itertools import islice
from typing import Annotated

from beets import config
from beets.dbcore.query import ParsingError
from fastapi import FastAPI, HTTPException, Query, Request, responses
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .imports import Imports
from .library import default_store_path, open_library, to_json
from .modify import Modifications
from .store import Store

log = logging.getLogger(__name__)


class JSONResponse(responses.JSONResponse):
    """Beets builds field names from a set, so their order differs between processes."""

    def render(self, content) -> bytes:
        return json.dumps(
            content, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()


class ModificationRequest(BaseModel):
    query: str
    album: bool = False
    set: dict[str, str] = Field(default_factory=dict)
    delete: list[str] = Field(default_factory=list)


class ImportRequest(BaseModel):
    paths: list[str]
    singletons: bool = False


class ChoiceRequest(BaseModel):
    prompt_id: str
    action: str
    candidate: int | None = None
    artist: str | None = None
    name: str | None = None
    search_ids: list[str] = Field(default_factory=list)


class MusicDirMiddleware:
    """Beets resolves stored relative paths through a ContextVar, set per context."""

    def __init__(self, app, lib):
        self.app = app
        self.lib = lib

    async def __call__(self, scope, receive, send):
        with self.lib.music_dir_context():
            await self.app(scope, receive, send)


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    lib = open_library()
    store_path = os.environ.get("BEETS_API_DB") or default_store_path()
    log.info("history: %s", store_path)
    store = Store(store_path)
    store.mark_interrupted(("queued", "applying", "running", "waiting_for_choice"))
    modifications = Modifications(lib, store)
    imports = Imports(lib, store)

    app = FastAPI(title="beets-api", default_response_class=JSONResponse)
    app.state.lib = lib
    # No auth: this stops websites reaching the API through DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_middleware(MusicDirMiddleware, lib=lib)

    @app.exception_handler(ParsingError)
    def invalid_query(_: Request, exc: ParsingError):
        return JSONResponse({"detail": f"invalid query: {exc}"}, status_code=400)

    @app.get("/list")
    def list_(
        query: str = "",
        album: bool = False,
        limit: Annotated[int, Query(ge=1)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        fields: str | None = None,
    ):
        results = lib.albums(query) if album else lib.items(query)
        names = fields.split(",") if fields else None
        return {
            "total": len(results),
            "limit": limit,
            "offset": offset,
            "results": [to_json(o, names) for o in islice(results, offset, offset + limit)],
        }

    @app.get("/items/{id_}")
    def get_item(id_: int):
        if (item := lib.get_item(id_)) is None:
            raise HTTPException(404, "item not found")
        return to_json(item)

    @app.get("/albums/{id_}")
    def get_album(id_: int):
        if (album := lib.get_album(id_)) is None:
            raise HTTPException(404, "album not found")
        return to_json(album)

    @app.get("/config")
    def get_config():
        return config.flatten(redact=True)

    @app.post("/modifications", status_code=201)
    def create_modification(body: ModificationRequest):
        return modifications.create(body.model_dump())

    @app.get("/modifications/{id_}")
    def get_modification(id_: str):
        return modifications.get(id_)

    @app.post("/modifications/{id_}/confirm", status_code=202)
    def confirm_modification(id_: str):
        return modifications.confirm(id_)

    @app.post("/imports", status_code=202)
    def create_import(body: ImportRequest):
        return imports.start(body.model_dump())

    @app.get("/imports/{id_}")
    def get_import(id_: str):
        return imports.get(id_)

    @app.post("/imports/{id_}/choice", status_code=202)
    def choose(id_: str, body: ChoiceRequest):
        return imports.choose(id_, body.model_dump())

    @app.delete("/imports/{id_}", status_code=202)
    def abort_import(id_: str):
        return imports.abort(id_)

    return app
