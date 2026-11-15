"""Import Paths: the directories in BEETS_API_IMPORT_PATHS that Imports may read from.

Browsing lists one directory level at a time, so the frontend can pick paths to
send to POST /imports. Only what an Import would accept is shown.
"""

import logging
import os
import stat
from datetime import UTC, datetime

from fastapi import HTTPException

from .roots import Roots

log = logging.getLogger(__name__)


def import_roots(value: str | None) -> Roots | None:
    """Parse BEETS_API_IMPORT_PATHS: directories joined by os.pathsep, like PATH.

    None means the variable is unset and Imports may read from anywhere.
    """
    if value is None:
        return None
    roots = Roots(p for p in value.split(os.pathsep) if p)
    for root in roots:
        if not os.path.isdir(root):
            log.warning("import path is not a directory: %s", root)
    return roots


def _entry(path: str) -> dict | None:
    """Type, size and mtime follow symlinks; None for anything but a file or directory."""
    try:
        st = os.stat(path)
    except OSError:  # A broken link, or deleted while listing.
        return None
    if stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        return None
    entry = {
        "name": os.path.basename(path),
        "path": path,
        "type": kind,
        "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
    }
    if kind == "file":
        entry["size"] = st.st_size
    return entry


def _sort_key(entry: dict):
    return (entry["type"] != "dir", entry["name"].casefold(), entry["name"])


def _kind(child: os.DirEntry, roots: Roots) -> str | None:
    """"dir" or "file" for what an Import would accept, else None.

    A real child of a directory that is inside the roots is inside them too, so
    only a symlink needs its target resolved. Both checks use what the directory
    listing already says, so no file is stat'ed until it is on the page.
    """
    try:
        if child.is_symlink() and not roots.contains(child.path):
            return None
        if child.is_dir():
            return "dir"
        if child.is_file():
            return "file"
    except OSError:  # deleted while listing
        pass
    return None


def _page(entries: list, limit: int, offset: int, path, parent) -> dict:
    return {
        "path": path, "parent": parent, "total": len(entries), "limit": limit, "offset": offset,
        "entries": entries[offset:offset + limit],
    }


def browse(roots: Roots | None, path: str | None, limit: int = 200, offset: int = 0) -> dict:
    """The Import Paths themselves without a path, otherwise a page of one directory's children.

    Only the page is stat'ed for its size and time, so a directory of tens of
    thousands of files is as quick to page through as a small one.
    """
    if path is None:
        entries = [e for root in roots or [] if (e := _entry(root)) and e["type"] == "dir"]
        return _page(sorted(entries, key=_sort_key), limit, offset, None, None)
    if not os.path.isabs(path):
        raise HTTPException(400, "path must be absolute")
    path = os.path.normpath(path)
    if roots is None or not roots.contains(path):
        raise HTTPException(403, "path is outside BEETS_API_IMPORT_PATHS")
    if not os.path.exists(path):
        raise HTTPException(404, "path not found")
    if not os.path.isdir(path):
        raise HTTPException(400, "path is not a directory")
    try:
        with os.scandir(path) as scan:
            children = [c for c in scan if not c.name.startswith(".")]
    except PermissionError:
        raise HTTPException(403, "directory is not readable") from None
    shown = [(c, kind) for c in children if (kind := _kind(c, roots))]
    shown.sort(key=lambda ck: (ck[1] != "dir", ck[0].name.casefold(), ck[0].name))
    parent = None if roots.is_root(path) else os.path.dirname(path)
    page = shown[offset:offset + limit]
    entries = [e for c, _ in page if (e := _entry(os.path.join(path, c.name)))]
    return {"path": path, "parent": parent, "total": len(shown), "limit": limit,
            "offset": offset, "entries": entries}
