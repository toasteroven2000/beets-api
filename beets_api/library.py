"""Opening the beets library and turning its records into JSON."""

import hashlib
import json
import logging
import os
import tempfile

from beets import config, plugins, util
from beets.library import Library

log = logging.getLogger(__name__)


def open_library() -> Library:
    """Open the library the same way `beet` does, using BEETSDIR."""
    config.clear()
    config.read()
    plugins.load_plugins()
    path = config["library"].as_filename()
    directory = config["directory"].as_filename()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lib = Library(path, directory)
    plugins.send("library_opened", lib=lib)
    log.info("library: %s, music: %s", path, directory)
    return lib


def forget_cached_paths(lib: Library) -> None:
    """Clear beets' memo of %aunique results.

    It lasts as long as the Library, which is harmless for a short CLI run but
    goes stale in a server, so files would move to outdated paths.
    """
    lib._memotable.clear()


def default_store_path() -> str:
    return os.path.join(config.config_dir(), "beets-api.db")


def json_value(value):
    return os.fsdecode(value) if isinstance(value, bytes) else value


def to_json(obj, fields: list[str] | None = None) -> dict:
    keys = obj.keys() if fields is None else [
        f for f in fields if f in obj.keys(computed=True)
    ]
    return {key: json_value(obj[key]) for key in keys}


def fingerprint(fields: dict) -> str:
    """Identify a record's state; `mtime` changes without its data changing."""
    data = {k: v for k, v in fields.items() if k != "mtime"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# Sniffed from the bytes rather than trusted from Content-Type.
IMAGE_SIGNATURES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


def image_extension(data: bytes) -> str | None:
    return next((ext for sig, ext in IMAGE_SIGNATURES.items() if data.startswith(sig)), None)


def set_album_art(album, data: bytes, extension: str) -> str | None:
    """Save the image as cover.jpg or cover.png in the Album's directory.

    Returns the previous artpath, whose file is deleted if it was another file.

    Always named `cover`, whatever beets' `art_filename` says; `beet move`
    would still rename it to that later.
    """
    directory = album.item_dir()
    dest = os.path.join(directory, b"cover" + extension.encode())
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
        f.write(data)
    os.replace(f.name, dest)  # atomic, so a reader never sees half an image
    if album.artpath and album.artpath != dest:
        util.remove(album.artpath)
    old = album.artpath
    album.artpath = dest
    album.store()
    # No "art_set" event: embedart would embed the image into the Items' files.
    return json_value(old)
