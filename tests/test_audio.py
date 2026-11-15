import os

import pytest

from conftest import restart


@pytest.fixture
def album(env):
    return env.seed_album()


def test_an_items_file_is_served_for_playing(env, album):
    item = album.items()[0]
    response = env.client.get(f"/items/{item.id}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == open(item.path, "rb").read()


def test_a_range_is_answered_so_the_browser_can_seek(env, album):
    item = album.items()[0]
    response = env.client.get(f"/items/{item.id}/audio", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.content == open(item.path, "rb").read()[:10]


def test_an_unknown_item_is_404(env):
    assert env.client.get("/items/999/audio").status_code == 404


def test_an_item_whose_file_is_gone_is_404(env, album):
    item = album.items()[0]
    os.remove(item.path)
    assert env.client.get(f"/items/{item.id}/audio").status_code == 404


def test_an_item_outside_the_music_directory_is_refused(env, album):
    item = album.items()[0]
    secret = env.tmp_path / "elsewhere" / "x.mp3"
    secret.parent.mkdir()
    secret.write_bytes(b"x")
    item.path = os.fsencode(secret)
    item.store()
    assert env.client.get(f"/items/{item.id}/audio").status_code == 403


def test_an_item_that_is_not_audio_is_refused(env, album):
    item = album.items()[0]
    other = os.path.join(os.path.dirname(item.path), b"notes.txt")
    open(other, "wb").write(b"secret")
    item.path = other
    item.store()
    assert env.client.get(f"/items/{item.id}/audio").status_code == 415


def configure(env, monkeypatch, *roots):
    monkeypatch.setenv("BEETS_API_IMPORT_PATHS", os.pathsep.join(str(r) for r in roots))
    restart(env)


@pytest.fixture
def root(env):
    root = env.tmp_path.resolve() / "incoming"
    root.mkdir()
    (root / "01.flac").write_bytes(b"flac data")
    (root / "notes.txt").write_bytes(b"secret")
    return root


def audio(env, path, status=200):
    response = env.client.get("/import-paths/audio", params={"path": str(path)})
    assert response.status_code == status, response.text
    return response


def test_a_file_under_an_import_path_is_served(env, root, monkeypatch):
    configure(env, monkeypatch, root)
    response = audio(env, root / "01.flac")
    assert response.headers["content-type"] == "audio/flac"
    assert response.content == b"flac data"


def test_files_outside_the_import_paths_are_refused(env, root, monkeypatch):
    configure(env, monkeypatch, root)
    audio(env, env.tmp_path / "other.mp3", status=403)
    audio(env, root / ".." / "other.mp3", status=403)


def test_unset_import_paths_serve_nothing(env, root):
    audio(env, root / "01.flac", status=403)


def test_only_audio_and_only_absolute_paths(env, root, monkeypatch):
    configure(env, monkeypatch, root)
    audio(env, root / "notes.txt", status=415)
    audio(env, root / "missing.mp3", status=404)
    audio(env, "01.flac", status=400)


def test_a_link_out_of_the_import_paths_is_refused(env, root, monkeypatch):
    outside = env.tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    os.symlink(outside, root / "link.mp3")
    configure(env, monkeypatch, root)
    audio(env, root / "link.mp3", status=403)
