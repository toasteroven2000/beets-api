import os
import threading

from mediafile import MediaFile

def test_list_items_uses_beets_query_syntax(env):
    env.seed_album("First", titles=("One", "Two"))
    env.seed_album("Second", titles=("Three",))
    body = env.client.get("/list", params={"scope": "item", "query": "album:Second"}).json()
    assert body["total"] == 1
    assert body["results"][0]["title"] == "Three"
    assert os.path.isabs(body["results"][0]["path"])


def test_list_albums_with_scope(env):
    env.seed_album("First")
    body = env.client.get("/list", params={"scope": "album"}).json()
    assert [a["album"] for a in body["results"]] == ["First"]


def test_list_requires_a_valid_scope(env):
    env.seed_album()
    assert env.client.get("/list").status_code == 422
    assert env.client.get("/list", params={"scope": "track"}).status_code == 422


def test_list_pages_and_narrows_fields(env):
    env.seed_album(titles=("One", "Two", "Three"))
    body = env.client.get(
        "/list", params={"scope": "item", "limit": 1, "offset": 1, "fields": "title,track"}
    ).json()
    assert body["total"] == 3
    assert len(body["results"]) == 1
    assert set(body["results"][0]) == {"title", "track"}


def test_list_rejects_invalid_query(env):
    assert env.client.get("/list", params={"scope": "item", "query": 'album:"unclosed'}).status_code == 400


def test_get_item_and_album(env):
    album = env.seed_album()
    item_id = album.items()[0].id
    assert env.client.get(f"/items/{item_id}").json()["id"] == item_id
    assert env.client.get(f"/albums/{album.id}").json()["album"] == "My Album"
    assert env.client.get("/items/999").status_code == 404
    assert env.client.get("/albums/999").status_code == 404


def test_rejects_other_hosts(env):
    assert env.client.get("/list?scope=item", headers={"Host": "evil.example"}).status_code == 400


def test_rejects_non_json_bodies(env):
    response = env.client.post(
        "/modifications",
        content='{"query": "", "set": {"title": "x"}}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 422


def test_paths_are_absolute_in_a_thread_without_inherited_context(env):
    # A real server handles requests outside the context that opened the library.
    album = env.seed_album(titles=("One",))
    result = {}

    def request():
        result["path"] = env.client.get(f"/items/{album.items()[0].id}").json()["path"]

    thread = threading.Thread(target=request)
    thread.start()
    thread.join()
    assert os.path.isabs(result["path"])


def assert_keys_sorted(value):
    if isinstance(value, dict):
        assert list(value) == sorted(value)
        value = list(value.values())
    if isinstance(value, list):
        for child in value:
            assert_keys_sorted(child)


def test_response_keys_are_sorted_at_every_level(env):
    album = env.seed_album(zebra="1", apple="2")
    for url in ("/list?scope=item", "/list?scope=album", "/items/1", f"/albums/{album.id}"):
        assert_keys_sorted(env.client.get(url).json())
    op = env.preview(scope="item", query="", set={"title": "New", "genres": "Rock"})
    assert op["result"]["preview"]["items"]
    assert_keys_sorted(op)
    assert_keys_sorted(env.client.get(f"/modifications/{op['id']}").json())


def test_config_shows_user_values_and_defaults_with_secrets_redacted(env):
    from beets import config

    config["import"]["copy"].set(True)
    config["some_secret"] = "hunter2"
    config["some_secret"].redact = True
    body = env.client.get("/config")
    assert body.status_code == 200
    assert "hunter2" not in body.text
    config_json = body.json()
    assert config_json["some_secret"] == "REDACTED"
    assert config_json["directory"] == "Music"  # from the user's config.yaml
    assert config_json["import"]["copy"] is True
    assert "sort_item" in config_json  # from beets' defaults


def test_album_art_serves_the_image_file(env):
    album = env.seed_album()
    image = env.tmp_path / "cover.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    album.set_art(str(image))
    album.store()
    response = env.client.get(f"/albums/{album.id}/art")
    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nfake"
    assert response.headers["content-type"] == "image/png"


def test_album_art_is_404_without_album_art_or_file(env):
    album = env.seed_album()
    assert env.client.get("/albums/999/art").status_code == 404
    assert env.client.get(f"/albums/{album.id}/art").json()["detail"] == "album has no art"
    image = env.tmp_path / "cover.jpg"
    image.write_bytes(b"jpeg")
    album.set_art(str(image))
    album.store()
    os.remove(album.artpath)
    response = env.client.get(f"/albums/{album.id}/art")
    assert response.status_code == 404
    assert response.json()["detail"] == "album art file is missing"


PNG = b"\x89PNG\r\n\x1a\n" + b"png data"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg data"


def test_upload_album_art_stores_it_beside_the_album(env):
    album = env.seed_album()
    response = env.client.put(f"/albums/{album.id}/art", content=PNG)
    assert response.status_code == 200, response.text
    artpath = response.json()["result"]["artpath"]
    assert os.path.dirname(artpath) == os.fsdecode(album.items()[0].path).rsplit("/", 1)[0]
    assert os.path.basename(artpath) == "cover.png"
    assert env.lib.get_album(album.id).artpath == os.fsencode(artpath)
    art = env.client.get(f"/albums/{album.id}/art")
    assert (art.content, art.headers["content-type"]) == (PNG, "image/png")


def test_upload_album_art_replaces_existing_art(env):
    album = env.seed_album()
    old = env.client.put(f"/albums/{album.id}/art", content=JPEG).json()["result"]["artpath"]
    new = env.client.put(f"/albums/{album.id}/art", content=PNG).json()["result"]["artpath"]
    assert (os.path.basename(old), os.path.basename(new)) == ("cover.jpg", "cover.png")
    assert not os.path.exists(old)
    assert env.client.get(f"/albums/{album.id}/art").content == PNG


def test_upload_album_art_is_named_cover_whatever_the_config(env):
    from beets import config

    config["art_filename"] = "folder"
    album = env.seed_album()
    artpath = env.client.put(f"/albums/{album.id}/art", content=JPEG).json()["result"]["artpath"]
    assert os.path.basename(artpath) == "cover.jpg"
    files = os.listdir(os.path.dirname(artpath))
    assert sorted(f for f in files if not f.endswith(".mp3")) == ["cover.jpg"]


def test_upload_album_art_rejects_bad_requests(env):
    album = env.seed_album()
    assert env.client.put("/albums/999/art", content=PNG).status_code == 404
    response = env.client.put(f"/albums/{album.id}/art", content=b"GIF89a not supported")
    assert response.status_code == 415
    assert env.client.put(f"/albums/{album.id}/art", content=b"").status_code == 415
    assert env.lib.get_album(album.id).artpath is None
    assert env.client.app.state.store.list("art_upload", [], 10, 0)[0] == 0


def test_upload_album_art_rejects_large_images(env, monkeypatch):
    monkeypatch.setattr("beets_api.app.MAX_ART_BYTES", len(PNG) - 1)
    album = env.seed_album()
    assert env.client.put(f"/albums/{album.id}/art", content=PNG).status_code == 413


def test_upload_album_art_is_recorded_in_history(env):
    album = env.seed_album()
    first = env.client.put(f"/albums/{album.id}/art", content=JPEG).json()
    second = env.client.put(f"/albums/{album.id}/art", content=PNG).json()

    assert type(second["id"]) is int and second["id"] > first["id"]
    assert (second["kind"], second["status"]) == ("art_upload", "applied")
    assert second["request"] == {"album_id": album.id, "format": "png", "bytes": len(PNG)}
    assert first["result"]["old_artpath"] is None
    assert second["result"]["old_artpath"] == first["result"]["artpath"]
    assert second["result"]["artpath"].endswith("/cover.png")

    total, ops = env.client.app.state.store.list("art_upload", [], 10, 0)
    assert total == 2 and ops[0] == second
    assert env.client.get("/modifications").json()["total"] == 0


def test_upload_album_art_does_not_embed_it(env, monkeypatch):
    sent = []
    monkeypatch.setattr("beets.plugins.send", lambda event, **kw: sent.append(event) or [])
    album = env.seed_album()
    assert env.client.put(f"/albums/{album.id}/art", content=PNG).status_code == 200
    assert "art_set" not in sent
    assert all(not MediaFile(item.path).images for item in env.lib.get_album(album.id).items())
