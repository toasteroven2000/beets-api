import errno
import os

import pytest
from fastapi import HTTPException

from beets_api.trash import Trash


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "music"
    (root / "Artist" / "Album").mkdir(parents=True)
    return root


def make(music, name="a.mp3", data=b"x"):
    path = music / "Artist" / "Album" / name
    path.parent.mkdir(parents=True, exist_ok=True)  # an earlier move may have pruned it
    path.write_bytes(data)
    return str(path)


def test_a_file_keeps_its_path_relative_to_the_music_directory(tmp_path, music):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    src = make(music)
    moved = trash.put("7", src)
    assert moved == {"from": src, "to": str(tmp_path / "trash" / "7" / "Artist" / "Album" / "a.mp3")}
    assert not os.path.exists(src) and os.path.exists(moved["to"])
    assert not (music / "Artist").exists()  # emptied directories are pruned


def test_a_name_already_in_the_trash_is_not_overwritten(tmp_path, music):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    first = trash.put("7", make(music, data=b"one"))
    second = trash.put("7", make(music, data=b"two"))
    assert first["to"] != second["to"]
    assert open(first["to"], "rb").read() == b"one" and open(second["to"], "rb").read() == b"two"


def test_put_all_leaves_files_outside_the_music_directory(tmp_path, music):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    outside = tmp_path / "elsewhere.mp3"
    outside.write_bytes(b"x")
    inside = [make(music), make(music, "b.mp3")]
    moved, left = trash.put_all("7", [inside[0], str(outside), None, inside[1], inside[0]])
    assert [m["from"] for m in moved] == inside  # each file once
    assert left == [str(outside)] and outside.exists()


def test_nothing_is_moved_and_nothing_is_required_when_there_is_nothing_to_move(tmp_path, music):
    trash = Trash(None, os.fsencode(music))
    assert trash.put_all("7", [None, str(music / "missing.mp3")]) == ([], [])


def test_unset_refuses_whenever_there_is_something_to_move(tmp_path, music):
    trash = Trash(None, os.fsencode(music))
    src = make(music)
    with pytest.raises(HTTPException) as caught:
        trash.put_all("7", [src])
    assert caught.value.status_code == 409 and os.path.exists(src)


def test_another_filesystem_copies_checks_then_deletes_the_original(tmp_path, music, monkeypatch):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    src = make(music, data=b"payload")

    def cross_device(*_):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "rename", cross_device)
    moved = trash.put("7", src)
    assert open(moved["to"], "rb").read() == b"payload" and not os.path.exists(src)


def test_an_incomplete_copy_leaves_the_original_in_place(tmp_path, music, monkeypatch):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    src = make(music, data=b"payload")
    monkeypatch.setattr(os, "rename", lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")))
    monkeypatch.setattr("shutil.copy2", lambda s, d: open(d, "wb").write(b"par"))
    with pytest.raises(OSError, match="incomplete"):
        trash.put("7", src)
    assert open(src, "rb").read() == b"payload"
    assert not list((tmp_path / "trash").rglob("a.mp3"))


def test_folders_report_files_bytes_and_a_changing_fingerprint(tmp_path, music):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    trash.put("7", make(music, data=b"1234"))
    (folder,) = trash.folders()
    assert (folder["name"], folder["files"], folder["bytes"]) == ("7", 1, 4)
    trash.put("7", make(music, "b.mp3", b"12"))
    assert trash.folders()[0]["fingerprint"] != folder["fingerprint"]


def test_purge_refuses_names_that_are_not_folders_of_the_trash(tmp_path, music):
    trash = Trash(str(tmp_path / "trash"), os.fsencode(music))
    trash.put("7", make(music))
    for name in ("..", "../music", "7/Artist", ""):
        with pytest.raises(ValueError):
            trash.purge(name)
    assert (music.parent / "music").exists()
    trash.purge("7")
    assert trash.folders() == []
