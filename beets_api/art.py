"""Album art changes, each written to the audit and none ever destructive.

Art is only ever a standalone file beside the Album's Items: nothing here
embeds it into the Items' files or reads a path from a request. Files are
only served, written or moved inside the music directory from beets' config,
so an artpath set some other way (e.g. `beet` or a hand-edited database)
can't expose or move anything else.
"""

import os
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException

from .caller import Caller
from .library import json_value
from .roots import Roots
from .store import Store

MAX_BYTES = 20 * 1024 * 1024

# Sniffed from the bytes rather than trusted from Content-Type.
IMAGE_SIGNATURES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


def image_extension(data: bytes) -> str | None:
    return next((ext for sig, ext in IMAGE_SIGNATURES.items() if data.startswith(sig)), None)


class AlbumArt:
    def __init__(self, lib, store: Store):
        self.lib = lib
        self.store = store
        # One change at a time, so two uploads can't archive the same file.
        self.lock = threading.Lock()
        self.music_dir = Roots([lib.directory])

    def _album(self, album_id: int):
        if (album := self.lib.get_album(album_id)) is None:
            raise HTTPException(404, "album not found")
        return album

    def file(self, album_id: int) -> Path:
        album = self._album(album_id)
        if (path := album.art_filepath) is None:
            raise HTTPException(404, "album has no art")
        if not self.music_dir.contains(album.artpath):
            raise HTTPException(403, "album art is outside the music directory")
        if not path.is_file():
            raise HTTPException(404, "album art file is missing")
        return path

    def _set_aside(self, album_id: int, paths) -> tuple[list[dict], list[str]]:
        """Archive files inside the music directory; leave any others untouched."""
        archived, left = [], []
        for path in dict.fromkeys(filter(None, paths)):
            if not self.music_dir.contains(path):
                left.append(json_value(path))
            elif entry := self._archive(album_id, path):
                archived.append(entry)
        return archived, left

    def _archive(self, album_id: int, path: bytes) -> dict | None:
        """Copy a replaced or removed image into the database, then delete the file."""
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        id_ = self.store.archive_art(album_id, json_value(path), data)
        os.remove(path)
        return {"id": id_, "from": json_value(path), "bytes": len(data)}

    def upload(self, album_id: int, data: bytes, caller: Caller) -> dict:
        """Save the image as cover.jpg or cover.png in the Album's directory.

        Always named `cover`, whatever beets' `art_filename` says; `beet move`
        would still rename it to that later. No "art_set" event is sent, as
        embedart would embed the image into the Items' files.
        """
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"album art is larger than {MAX_BYTES} bytes")
        if (extension := image_extension(data)) is None:
            raise HTTPException(415, "album art must be a JPEG or PNG image")
        with self.lock:
            album = self._album(album_id)
            old = album.artpath
            directory = album.item_dir()
            if not self.music_dir.contains(directory):
                raise HTTPException(403, "album is outside the music directory")
            dest = os.path.join(directory, b"cover" + extension.encode())
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
                f.write(data)
            # A file already at dest may not be the recorded art; keep it too.
            archived, left = self._set_aside(album_id, (old, dest))
            os.replace(f.name, dest)
            album.artpath = dest
            album.store()
            changes = {
                "artpath": {"old": json_value(old), "new": json_value(dest)},
                "format": extension[1:],
                "bytes": len(data),
                "archived": archived,
                "left_in_place": left,
            }
            return self.store.audit(
                "art.upload", f"album:{album_id}", changes, caller
            )

    def remove(self, album_id: int, caller: Caller) -> dict | None:
        """Clear the Album's art. Removing art that isn't there changes nothing."""
        with self.lock:
            album = self._album(album_id)
            if not (old := album.artpath):
                return None
            archived, left = self._set_aside(album_id, (old,))
            album.artpath = None
            album.store()
            changes = {
                "artpath": {"old": json_value(old), "new": None},
                "archived": archived,
                "left_in_place": left,
            }
            return self.store.audit(
                "art.remove", f"album:{album_id}", changes, caller
            )
