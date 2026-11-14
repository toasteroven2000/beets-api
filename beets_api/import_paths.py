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


def browse(roots: Roots | None, path: str | None) -> dict:
    """The Import Paths themselves without a path, otherwise one directory's children."""
    if path is None:
        entries = [e for root in roots or [] if (e := _entry(root)) and e["type"] == "dir"]
        return {"path": None, "parent": None, "entries": sorted(entries, key=_sort_key)}
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
        names = os.listdir(path)
    except PermissionError:
        raise HTTPException(403, "directory is not readable") from None
    children = (os.path.join(path, n) for n in names if not n.startswith("."))
    entries = [e for p in children if roots.contains(p) and (e := _entry(p))]
    parent = None if roots.is_root(path) else os.path.dirname(path)
    return {"path": path, "parent": parent, "entries": sorted(entries, key=_sort_key)}
