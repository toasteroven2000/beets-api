from dataclasses import asdict

from beets_api.changes import AlbumImport, ArtUpload, ItemImport, OldNew, Replaced, Undone


def test_stored_keys_are_the_documented_ones():
    keys = lambda c: set(asdict(c))
    assert keys(ArtUpload(OldNew(None, "p"), "jpg", 1, [], [])) == {
        "artpath", "format", "bytes", "archived", "left_in_place"}
    assert keys(AlbumImport([], "asis", [])) == {"source", "match", "items"}
    assert keys(ItemImport([], "asis", "p")) == {"source", "match", "path"}
    assert keys(Replaced({}, "album:1")) == {"removed", "replaced_by"}
    assert keys(Undone({}, [], 1)) == {"removed", "files", "undo_of_import"}


def test_reading_back_what_was_stored_round_trips():
    written = asdict(AlbumImport(["/in"], {"data_source": "x", "id": "1"}, [{"id": 1, "path": "p"}]))
    assert AlbumImport(**written).items == [{"id": 1, "path": "p"}]
