import os
import threading

def test_list_items_uses_beets_query_syntax(env):
    env.seed_album("First", titles=("One", "Two"))
    env.seed_album("Second", titles=("Three",))
    body = env.client.get("/list", params={"query": "album:Second"}).json()
    assert body["total"] == 1
    assert body["results"][0]["title"] == "Three"
    assert os.path.isabs(body["results"][0]["path"])


def test_list_albums_with_flag(env):
    env.seed_album("First")
    body = env.client.get("/list", params={"album": True}).json()
    assert [a["album"] for a in body["results"]] == ["First"]


def test_list_pages_and_narrows_fields(env):
    env.seed_album(titles=("One", "Two", "Three"))
    body = env.client.get(
        "/list", params={"limit": 1, "offset": 1, "fields": "title,track"}
    ).json()
    assert body["total"] == 3
    assert len(body["results"]) == 1
    assert set(body["results"][0]) == {"title", "track"}


def test_list_rejects_invalid_query(env):
    assert env.client.get("/list", params={"query": 'album:"unclosed'}).status_code == 400


def test_get_item_and_album(env):
    album = env.seed_album()
    item_id = album.items()[0].id
    assert env.client.get(f"/items/{item_id}").json()["id"] == item_id
    assert env.client.get(f"/albums/{album.id}").json()["album"] == "My Album"
    assert env.client.get("/items/999").status_code == 404
    assert env.client.get("/albums/999").status_code == 404


def test_rejects_other_hosts(env):
    assert env.client.get("/list", headers={"Host": "evil.example"}).status_code == 400


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
