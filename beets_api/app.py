"""HTTP routes. Run with:

python -m uvicorn --factory beets_api.app:create_app --host 127.0.0.1 --port 8338
"""

import json
import logging
import os
from itertools import islice
from typing import Annotated, Literal

from beets import config
from beets.dbcore.query import ParsingError
from fastapi import FastAPI, HTTPException, Query, Request, responses
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .imports import Imports
from .library import (
    default_store_path,
    image_extension,
    json_value,
    open_library,
    set_album_art,
    to_json,
)
from .modify import Modifications
from .store import Store

log = logging.getLogger(__name__)

MAX_ART_BYTES = 20 * 1024 * 1024


class JSONResponse(responses.JSONResponse):
    """Beets builds field names from a set, so their order differs between processes."""

    def render(self, content) -> bytes:
        return json.dumps(
            content, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()


class ModificationRequest(BaseModel):
    scope: Literal["item", "album"]
    query: str
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
    app.state.store = store
    # No auth: this stops websites reaching the API through DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_middleware(MusicDirMiddleware, lib=lib)

    @app.exception_handler(ParsingError)
    def invalid_query(_: Request, exc: ParsingError):
        return JSONResponse({"detail": f"invalid query: {exc}"}, status_code=400)

    @app.get("/list")
    def list_(
        scope: Literal["item", "album"],
        query: str = "",
        limit: Annotated[int, Query(ge=1)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        fields: str | None = None,
    ):
        results = lib.albums(query) if scope == "album" else lib.items(query)
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

    @app.get("/albums/{id_}/art", response_class=responses.FileResponse)
    def get_album_art(id_: int):
        if (album := lib.get_album(id_)) is None:
            raise HTTPException(404, "album not found")
        if (path := album.art_filepath) is None:
            raise HTTPException(404, "album has no art")
        if not path.is_file():
            raise HTTPException(404, "album art file is missing")
        return responses.FileResponse(path)

    @app.put("/albums/{id_}/art")
    async def put_album_art(id_: int, request: Request):
        """The body is the image itself (JPEG or PNG), not a multipart form."""
        data = await request.body()
        if len(data) > MAX_ART_BYTES:
            raise HTTPException(413, f"album art is larger than {MAX_ART_BYTES} bytes")
        if (extension := image_extension(data)) is None:
            raise HTTPException(415, "album art must be a JPEG or PNG image")

        def upload():
            if (album := lib.get_album(id_)) is None:
                raise HTTPException(404, "album not found")
            old = set_album_art(album, data, extension)
            request = {"album_id": id_, "format": extension[1:], "bytes": len(data)}
            result = {"old_artpath": old, "artpath": json_value(album.artpath)}
            return store.get(store.create("art_upload", "applied", request, result), "art_upload")

        return await run_in_threadpool(upload)

    @app.get("/config")
    def get_config():
        return config.flatten(redact=True)

    @app.post("/modifications", status_code=201)
    def create_modification(body: ModificationRequest):
        return modifications.create(body.model_dump())

    @app.get("/modifications")
    def list_modifications(
        status: Annotated[list[str], Query()] = [],
        limit: Annotated[int, Query(ge=1)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return modifications.list(status, limit, offset)

    @app.get("/modifications/{id_}")
    def get_modification(id_: int):
        return modifications.get(id_)

    @app.post("/modifications/{id_}/confirm", status_code=202)
    def confirm_modification(id_: int):
        return modifications.confirm(id_)

    @app.post("/imports", status_code=202)
    def create_import(body: ImportRequest):
        return imports.start(body.model_dump())

    @app.get("/imports/{id_}")
    def get_import(id_: int):
        return imports.get(id_)

    @app.post("/imports/{id_}/choice", status_code=202)
    def choose(id_: int, body: ChoiceRequest):
        return imports.choose(id_, body.model_dump())

    @app.delete("/imports/{id_}", status_code=202)
    def abort_import(id_: int):
        return imports.abort(id_)

    return app
