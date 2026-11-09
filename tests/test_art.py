import os
import sqlite3

import pytest
from mediafile import MediaFile

PNG = b"\x89PNG\r\n\x1a\n" + b"png data"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg data"
JPEG2 = b"\xff\xd8\xff\xe0" + b"other jpeg"


def upload(env, album, data, client=None):
    headers = {"X-Client": client} if client else {}
    response = env.client.put(f"/albums/{album.id}/art", content=data, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def remove(env, album, client=None):
    headers = {"X-Client": client} if client else {}
    response = env.client.delete(f"/albums/{album.id}/art", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def new_artpath(entry):
    return entry["changes"]["artpath"]["new"]


def audit(env, **params):
    return env.client.get("/audit", params=params).json()


def archived(env, entry):
    """The bytes of each image an Audit entry says it archived."""
    with sqlite3.connect(env.client.app.state.store.path) as conn:
        return [
            conn.execute("SELECT data FROM art_archive WHERE id = ?", (a["id"],)).fetchone()[0]
            for a in entry["changes"]["archived"]
        ]


def album_dir(album):
    return os.fsdecode(os.path.dirname(album.items()[0].path))


def read(path):
    with open(path, "rb") as f:
        return f.read()


# Uploading


def test_upload_stores_cover_beside_the_album_and_serves_it(env):
    album = env.seed_album()
    op = upload(env, album, PNG)
    artpath = new_artpath(op)
    assert artpath == os.path.join(album_dir(album), "cover.png")
    assert env.lib.get_album(album.id).artpath == os.fsencode(artpath)
    art = env.client.get(f"/albums/{album.id}/art")
    assert (art.content, art.headers["content-type"]) == (PNG, "image/png")


def test_upload_is_named_cover_whatever_the_config(env):
    from beets import config

    config["art_filename"] = "folder"
    album = env.seed_album()
    artpath = new_artpath(upload(env, album, JPEG))
    assert os.path.basename(artpath) == "cover.jpg"
    files = os.listdir(os.path.dirname(artpath))
    assert sorted(f for f in files if not f.endswith(".mp3")) == ["cover.jpg"]


def test_upload_rejects_bad_requests_without_recording_them(env, monkeypatch):
    album = env.seed_album()
    assert env.client.put("/albums/999/art", content=PNG).status_code == 404
    response = env.client.put(f"/albums/{album.id}/art", content=b"GIF89a not supported")
    assert response.status_code == 415
    assert env.client.put(f"/albums/{album.id}/art", content=b"").status_code == 415
    monkeypatch.setattr("beets_api.art.MAX_BYTES", len(PNG) - 1)
    assert env.client.put(f"/albums/{album.id}/art", content=PNG).status_code == 413
    assert env.lib.get_album(album.id).artpath is None
    assert audit(env)["total"] == 0


def test_upload_does_not_embed_it(env, monkeypatch):
    sent = []
    monkeypatch.setattr("beets.plugins.send", lambda event, **kw: sent.append(event) or [])
    album = env.seed_album()
    upload(env, album, PNG)
    assert "art_set" not in sent
    assert all(not MediaFile(item.path).images for item in env.lib.get_album(album.id).items())


# Archiving instead of deleting


def test_replacing_art_archives_the_old_image(env):
    album = env.seed_album()
    first = upload(env, album, JPEG)
    second = upload(env, album, PNG)

    old = new_artpath(first)
    assert not os.path.exists(old)
    [entry] = second["changes"]["archived"]
    assert (entry["from"], entry["bytes"]) == (old, len(JPEG))
    assert archived(env, second) == [JPEG]
    assert second["changes"]["artpath"]["old"] == old


def test_replacing_art_with_the_same_format_archives_the_old_image(env):
    album = env.seed_album()
    upload(env, album, JPEG)
    op = upload(env, album, JPEG2)
    assert archived(env, op) == [JPEG]
    assert read(new_artpath(op)) == JPEG2


def test_upload_archives_a_stray_cover_it_would_overwrite(env):
    album = env.seed_album()
    stray = os.path.join(album_dir(album), "cover.jpg")
    with open(stray, "wb") as f:
        f.write(b"not recorded in beets")
    op = upload(env, album, JPEG)
    assert archived(env, op) == [b"not recorded in beets"]


def test_archive_is_in_the_db_and_append_only(env):
    album = env.seed_album()
    upload(env, album, JPEG)
    upload(env, album, PNG)
    store_dir = os.path.dirname(os.path.abspath(env.client.app.state.store.path))
    assert not os.path.exists(os.path.join(store_dir, "art-archive"))
    with sqlite3.connect(env.client.app.state.store.path) as conn:
        assert conn.execute("SELECT album_id FROM art_archive").fetchall() == [(album.id,)]
        for sql in ("UPDATE art_archive SET data = x''", "DELETE FROM art_archive"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)


# Removing


def test_remove_clears_art_and_archives_the_file(env):
    album = env.seed_album()
    artpath = new_artpath(upload(env, album, PNG))

    op = remove(env, album)

    assert (op["action"], op["target"]) == ("art.remove", f"album:{album.id}")
    assert op["changes"]["artpath"] == {"old": artpath, "new": None}
    assert not os.path.exists(artpath)
    assert archived(env, op) == [PNG]
    assert env.lib.get_album(album.id).artpath is None
    assert env.client.get(f"/albums/{album.id}/art").status_code == 404


def test_remove_without_art_changes_nothing_and_is_not_audited(env):
    album = env.seed_album()
    assert env.client.delete(f"/albums/{album.id}/art").status_code == 204
    assert env.client.delete("/albums/999/art").status_code == 404
    assert audit(env)["total"] == 0


def test_remove_when_the_file_is_already_missing(env):
    album = env.seed_album()
    artpath = new_artpath(upload(env, album, PNG))
    os.remove(artpath)
    op = remove(env, album)
    assert op["changes"] == {
        "artpath": {"old": artpath, "new": None}, "archived": [], "left_in_place": []
    }
    assert env.lib.get_album(album.id).artpath is None


def test_remove_leaves_the_items_files_alone(env):
    album = env.seed_album()
    upload(env, album, PNG)
    before = {i.id: os.path.getmtime(i.path) for i in album.items()}
    remove(env, album)
    assert {i.id: os.path.getmtime(i.path) for i in album.items()} == before


# Audit


def test_every_art_change_is_audited_with_its_client(env):
    album = env.seed_album()
    added = upload(env, album, JPEG, client="beets-ui")
    replaced = upload(env, album, PNG)
    removed = remove(env, album, client="script")

    body = audit(env, target=f"album:{album.id}")
    assert body["results"] == [removed, replaced, added]
    assert [e["action"] for e in body["results"]] == ["art.remove", "art.upload", "art.upload"]
    assert [e["client"] for e in body["results"]] == ["script", None, "beets-ui"]
    assert added["operation_id"] is None
    assert added["changes"]["artpath"]["old"] is None
    assert (added["changes"]["format"], added["changes"]["bytes"]) == ("jpg", len(JPEG))
    assert replaced["changes"]["artpath"]["old"] == new_artpath(added)


# Confined to the music directory


def outside_file(env, name="secret.jpg", data=JPEG):
    path = env.tmp_path / "outside" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return str(path)


def set_artpath(env, album, path):
    album = env.lib.get_album(album.id)
    album.artpath = os.fsencode(path)
    album.store()


def test_art_outside_the_music_dir_is_not_served(env):
    album = env.seed_album()
    set_artpath(env, album, outside_file(env))
    response = env.client.get(f"/albums/{album.id}/art")
    assert response.status_code == 403
    assert JPEG not in response.content


def test_art_symlinked_out_of_the_music_dir_is_not_served(env):
    album = env.seed_album()
    link = os.path.join(album_dir(album), "cover.jpg")
    os.symlink(outside_file(env), link)
    set_artpath(env, album, link)
    assert env.client.get(f"/albums/{album.id}/art").status_code == 403


def test_upload_is_refused_for_an_album_outside_the_music_dir(env):
    folder = env.tmp_path / "elsewhere"
    from conftest import make_album_files
    from beets.library import Item

    items = [Item.from_path(f) for f in make_album_files(str(folder))]
    album = env.lib.add_album(items)  # added in place, never moved into the library
    response = env.client.put(f"/albums/{album.id}/art", content=JPEG)
    assert response.status_code == 403
    assert not (folder / "cover.jpg").exists()
    assert env.lib.get_album(album.id).artpath is None
    assert audit(env)["total"] == 0


def test_upload_leaves_old_art_outside_the_music_dir_in_place(env):
    album = env.seed_album()
    secret = outside_file(env)
    set_artpath(env, album, secret)
    op = upload(env, album, PNG)
    assert read(secret) == JPEG
    assert op["changes"]["archived"] == []
    assert op["changes"]["left_in_place"] == [secret]
    assert new_artpath(op) == os.path.join(album_dir(album), "cover.png")


def test_remove_clears_art_outside_the_music_dir_without_touching_it(env):
    album = env.seed_album()
    secret = outside_file(env)
    set_artpath(env, album, secret)
    op = remove(env, album)
    assert op["changes"] == {
        "artpath": {"old": secret, "new": None}, "archived": [], "left_in_place": [secret]
    }
    assert read(secret) == JPEG
    assert env.lib.get_album(album.id).artpath is None
