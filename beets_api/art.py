"""Album art changes, each written to the audit. A replaced or removed image goes to the Trash.

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
from .changes import ArtRemoval, ArtUpload, OldNew
from .library import json_value
from .roots import Roots
from .store import Store
from .trash import Trash, art_folder

MAX_BYTES = 20 * 1024 * 1024

# Sniffed from the bytes rather than trusted from Content-Type.
IMAGE_SIGNATURES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


def image_extension(data: bytes) -> str | None:
    return next((ext for sig, ext in IMAGE_SIGNATURES.items() if data.startswith(sig)), None)


def audio_mode(album) -> int:
    """The permissions of the Album's audio files, without execute bits."""
    for item in album.items():
        try:
            return os.stat(item.path).st_mode & 0o666
        except OSError:
            continue
    return 0o644


class AlbumArt:
    def __init__(self, lib, store: Store, trash: Trash):
        self.lib = lib
        self.store = store
        self.trash = trash
        # One change at a time, so two uploads can't move the same file.
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
                # The temporary file is created owner-only; without this the
                # cover would be unreadable to anyone the audio files are shared with.
                os.fchmod(f.fileno(), audio_mode(album))
            # A file already at dest may not be the recorded art; keep it too.
            try:
                trashed, left = self.trash.put_all(art_folder(album_id), (old, dest))
            except Exception:
                os.remove(f.name)
                raise
            os.replace(f.name, dest)
            album.artpath = dest
            album.store()
            changes = ArtUpload(
                OldNew(json_value(old), json_value(dest)), extension[1:], len(data), trashed, left
            )
            return self.store.audit(
                "art.upload", f"album:{album_id}", changes, caller
            )

    def remove(self, album_id: int, caller: Caller) -> dict | None:
        """Clear the Album's art. Removing art that isn't there changes nothing."""
        with self.lock:
            album = self._album(album_id)
            if not (old := album.artpath):
                return None
            trashed, left = self.trash.put_all(art_folder(album_id), (old,))
            album.artpath = None
            album.store()
            changes = ArtRemoval(OldNew(json_value(old), None), trashed, left)
            return self.store.audit(
                "art.remove", f"album:{album_id}", changes, caller
            )
