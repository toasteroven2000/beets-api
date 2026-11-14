"""Roots: directories that paths must stay inside.

The one place that decides whether a path is inside, so every caller resolves
symlinks the same way and a link can't point outside.
"""

import os
from collections.abc import Iterable, Iterator


class Roots:
    def __init__(self, paths: Iterable[str | bytes]):
        """Resolved once here, so later changes to a link can't move a root."""
        self._roots = [os.path.realpath(os.fsdecode(p)) for p in paths]

    def __iter__(self) -> Iterator[str]:
        return iter(self._roots)

    def __repr__(self) -> str:
        return f"Roots({self._roots!r})"

    def contains(self, path: str | bytes) -> bool:
        """Whether path, after resolving symlinks, is a root or inside one."""
        real = os.path.realpath(os.fsdecode(path))
        return any(os.path.commonpath([root, real]) == root for root in self._roots)

    def is_root(self, path: str | bytes) -> bool:
        return os.path.realpath(os.fsdecode(path)) in self._roots
