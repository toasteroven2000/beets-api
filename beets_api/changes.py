"""The `changes` of each kind of Audit entry, defined once.

Writers build these and the Audit stores them as JSON; undo reads them back
with the same classes, so a field added or renamed on one side fails loudly on
the other instead of being missed. The JSON keys are the field names.

A Modification's changes are not here: they are a diff keyed by whatever field
names the Modification touched, `{field: {"old": ..., "new": ...}}`.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OldNew:
    old: str | None
    new: str | None


@dataclass(frozen=True)
class ArtUpload:  # art.upload
    artpath: OldNew
    format: str
    bytes: int
    archived: list[dict]
    left_in_place: list[str]


@dataclass(frozen=True)
class ArtRemoval:  # art.remove
    artpath: OldNew
    archived: list[dict]
    left_in_place: list[str]


@dataclass(frozen=True)
class AlbumImport:  # album.import
    source: list[str]
    match: dict | str  # {"data_source", "id"}, or "asis"
    items: list[dict]  # {"id", "path"} of each Item added


@dataclass(frozen=True)
class ItemImport:  # item.import
    source: list[str]
    match: dict | str
    path: str


@dataclass(frozen=True)
class Replaced:  # album.remove / item.remove, by an Import replacing duplicates
    removed: dict
    replaced_by: str


@dataclass(frozen=True)
class Undone:  # album.remove / item.remove, by undoing an Import
    removed: dict
    files: list[str]
    undo_of_import: int
