"""The Trash: where files go instead of being deleted, until a Purge removes them.

BEETS_API_TRASH_DIR names it, and it is never created by default. While it is
unset every action that would destroy a file is refused (`require`).

Each operation gets one folder, named by its id, that keeps every file's path
relative to the music directory. Art changes made directly have no operation,
so they get a folder of their own.
"""

import errno
import hashlib
import logging
import os
import shutil
from datetime import UTC, datetime

from beets import util
from fastapi import HTTPException

from .library import json_value
from .roots import Roots

log = logging.getLogger(__name__)


def art_folder(album_id: int) -> str:
    """A folder name for art changed outside any operation."""
    return f"art-album-{album_id}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"


class Trash:
    def __init__(self, directory: str | None, music_dir: bytes | str):
        self.directory = os.path.abspath(directory) if directory else None
        self.music_dir = os.fsdecode(music_dir)
        self._music = Roots([music_dir])
        if self.directory:
            os.makedirs(self.directory, exist_ok=True)

    @property
    def configured(self) -> bool:
        return self.directory is not None

    def require(self) -> None:
        if not self.configured:
            raise HTTPException(409, "the Trash is not configured: set BEETS_API_TRASH_DIR")

    def warn_if_inside(self, roots: Roots | None) -> None:
        """A Trash inside the music directory or an Import Path could be imported again."""
        if not self.configured:
            return
        if self._music.contains(self.directory):
            log.warning("BEETS_API_TRASH_DIR is inside the music directory: %s", self.directory)
        if roots is not None and roots.contains(self.directory):
            log.warning("BEETS_API_TRASH_DIR is inside an Import Path: %s", self.directory)

    def _destination(self, folder: str, src: str) -> str:
        parent = os.path.realpath(os.path.dirname(src))
        relative = os.path.relpath(parent, os.path.realpath(self.music_dir))
        if relative == ".." or relative.startswith(".." + os.sep):
            raise ValueError(f"not inside the music directory: {src}")
        base = os.path.join(self.directory, folder, relative, os.path.basename(src))
        dest, n = base, 0
        while os.path.lexists(dest):
            n += 1
            dest = f"{base}.{n}"
        return dest

    def _move(self, src: str, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.rename(src, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Another filesystem: the original goes only once the copy is checked.
            shutil.copy2(src, dest)
            if os.path.getsize(dest) != os.path.getsize(src):
                os.remove(dest)
                raise OSError(f"copy of {src} to the Trash is incomplete; it was left in place") from None
            os.remove(src)

    def put(self, folder: str, path: str | bytes) -> dict | None:
        """Move one file in. None when there is no such file."""
        self.require()
        src = os.fsdecode(path)
        if not os.path.isfile(src):
            return None
        dest = self._destination(folder, src)
        self._move(src, dest)
        util.prune_dirs(os.fsencode(os.path.dirname(src)), root=os.fsencode(self.music_dir))
        return {"from": src, "to": dest}

    def put_all(self, folder: str, paths) -> tuple[list[dict], list[str]]:
        """Move the existing files inside the music directory in; return (moved, left_in_place).

        Files outside the music directory are never touched. Refused when there
        is something to move and the Trash is not configured.
        """
        moved, left, todo = [], [], []
        for path in dict.fromkeys(filter(None, paths)):
            if not self._music.contains(path):
                left.append(json_value(path))
            elif os.path.isfile(path):
                todo.append(path)
        if todo:
            self.require()
        for path in todo:
            if entry := self.put(folder, path):
                moved.append(entry)
        return moved, left

    def folders(self) -> list[dict]:
        """Every folder with its size, age and a fingerprint of what is in it."""
        if not self.configured:
            return []
        found = []
        for name in sorted(os.listdir(self.directory)):
            root = os.path.join(self.directory, name)
            if not os.path.isdir(root):
                continue
            files, size, newest, digest = 0, 0, os.stat(root).st_mtime, hashlib.sha256()
            for where, _, names in os.walk(root):
                for n in sorted(names):
                    st = os.stat(os.path.join(where, n))
                    files, size, newest = files + 1, size + st.st_size, max(newest, st.st_mtime)
                    digest.update(f"{os.path.relpath(os.path.join(where, n), root)}\0{st.st_size}\n".encode())
            found.append({
                "name": name, "files": files, "bytes": size,
                "modified": datetime.fromtimestamp(newest, UTC).isoformat(),
                "fingerprint": digest.hexdigest(),
            })
        return found

    def purge(self, name: str) -> None:
        """Delete one folder for good."""
        self.require()
        if name in ("", ".", "..") or name != os.path.basename(name):
            raise ValueError(f"not a Trash folder: {name!r}")
        path = os.path.join(self.directory, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
