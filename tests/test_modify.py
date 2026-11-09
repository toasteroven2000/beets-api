import os

from mediafile import MediaFile

from conftest import restart


def fresh_album(env, album):
    return env.lib.get_album(album.id)


def test_album_preview_changes_nothing_then_confirm_applies(env):
    album = env.seed_album(albumdisambig="old")
    before = {i.id: i.path for i in album.items()}

    op = env.preview(scope="album", query='album:"My Album"', set={"albumdisambig": ""})

    assert op["status"] == "awaiting_confirmation"
    assert op["result"]["preview"]["albums"][0]["changes"] == {
        "albumdisambig": {"old": "old", "new": ""}
    }
    assert len(op["result"]["preview"]["items"]) == 2
    assert fresh_album(env, album).albumdisambig == "old"
    assert {i.albumdisambig for i in fresh_album(env, album).items()} == {"old"}

    done = env.confirm(op["id"])

    assert done["status"] == "applied"
    assert done["result"]["failed"] == []
    assert fresh_album(env, album).albumdisambig == ""
    assert {i.albumdisambig for i in fresh_album(env, album).items()} == {""}
    assert {i.id: i.path for i in fresh_album(env, album).items()} == before


def test_preview_shows_new_path_and_confirm_moves_and_writes(env):
    album = env.seed_album()
    op = env.preview(scope="album", query="", set={"album": "Renamed"})
    items = op["result"]["preview"]["items"]
    expected = {e["id"]: e["changes"]["path"]["new"] for e in items}
    assert all("/Renamed/" in p for p in expected.values())

    assert env.confirm(op["id"])["status"] == "applied"

    for item in fresh_album(env, album).items():
        assert os.fsdecode(item.path) == expected[item.id]
        assert MediaFile(item.path).album == "Renamed"


def test_aunique_uses_old_value_in_preview_but_real_path_is_applied_and_reported(env):
    # Two same-named Albums: %aunique uses albumdisambig in their paths. It reads
    # the database, so the Preview shows a path built from the old value.
    first = env.seed_album(albumdisambig="alpha", titles=("One",))
    env.seed_album(albumdisambig="beta", titles=("Two",))
    op = env.preview(scope="album", query=f"id:{first.id}", set={"albumdisambig": "gamma"})
    previewed = op["result"]["preview"]["items"][0]["changes"]["path"]["new"]
    assert "alpha" in previewed

    done = env.confirm(op["id"])

    assert done["status"] == "applied"
    moved = fresh_album(env, first).items()[0]
    assert "gamma" in os.fsdecode(moved.path)
    assert os.path.exists(moved.path)
    reported = [a["path"] for a in done["result"]["applied"] if a["kind"] == "items"]
    assert reported == [os.fsdecode(moved.path)]


def test_preview_does_not_write_to_library_or_files(env):
    album = env.seed_album()
    path = album.items()[0].path
    mtime = os.path.getmtime(path)
    env.preview(scope="album", query="", set={"album": "Renamed", "mood": "calm"})
    assert fresh_album(env, album).album == "My Album"
    assert "mood" not in fresh_album(env, album)
    assert {i.album for i in fresh_album(env, album).items()} == {"My Album"}
    assert os.path.getmtime(path) == mtime
    assert os.path.exists(path)


def test_album_flexible_field_delete_reaches_items_in_preview(env):
    album = env.seed_album(titles=("One",))
    first = env.preview(scope="album", query="", set={"mood": "calm"})
    assert first["result"]["preview"]["items"][0]["changes"]["mood"]["new"] == "calm"
    assert env.confirm(first["id"])["status"] == "applied"
    op = env.preview(scope="album", query="", delete=["mood"])
    assert op["result"]["preview"]["items"][0]["changes"]["mood"] == {"old": "calm", "new": None}
    assert env.confirm(op["id"])["status"] == "applied"
    assert "mood" not in fresh_album(env, album).items()[0]


def test_query_matching_more_than_expected_is_visible(env):
    env.seed_album("My Album", titles=("One",))
    env.seed_album("My Album (Deluxe)", titles=("Two",))
    op = env.preview(scope="album", query="album:My Album", set={"comments": "x"})
    assert len(op["result"]["preview"]["albums"]) + len(op["result"]["preview"]["items"]) >= 2


def test_item_scope_with_template_and_delete(env):
    env.seed_album()
    first = env.preview(scope="item", query="title:One", set={"mood": "calm"})
    assert env.confirm(first["id"])["status"] == "applied"

    op = env.preview(scope="item", query="title:One", set={"title": "$title!"}, delete=["mood"])
    changes = op["result"]["preview"]["items"][0]["changes"]
    assert changes["title"] == {"old": "One", "new": "One!"}
    assert changes["mood"] == {"old": "calm", "new": None}
    assert env.confirm(op["id"])["status"] == "applied"
    item = env.lib.items("title:One!").get()
    assert item is not None
    assert "mood" not in item


def test_multi_value_operator_uses_beets_rules(env):
    env.seed_album(titles=("One",))
    op = env.preview(scope="item", query="", set={"genres+": "Rock"})
    assert op["result"]["preview"]["items"][0]["changes"]["genres"]["new"] == ["Rock"]
    response = env.client.post("/modifications", json={"scope": "item", "query": "", "set": {"title+": "x"}})
    assert response.status_code == 422


def test_no_changes_cannot_be_confirmed(env):
    env.seed_album(albumdisambig="")
    for body in (
        {"scope": "album", "query": "", "set": {"albumdisambig": ""}},
        {"scope": "item", "query": "title:nothing-matches", "set": {"title": "x"}},
    ):
        op = env.preview(**body)
        assert op["status"] == "no_changes"
        assert env.client.post(f"/modifications/{op['id']}/confirm").status_code == 409


def test_rejects_bad_requests(env):
    post = lambda **b: env.client.post("/modifications", json=b).status_code
    assert post(scope="item", query="", set={"id": "2"}) == 422
    assert post(scope="item", query="", set={"path": "/tmp/x"}) == 422
    assert post(scope="item", query="", delete=["path"]) == 422
    assert post(scope="item", query="") == 422
    assert post(scope="item", query='album:"unclosed', set={"title": "x"}) == 400
    assert post(query="", set={"title": "x"}) == 422
    assert post(scope="track", query="", set={"title": "x"}) == 422


def test_confirm_is_single_use(env):
    env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    env.confirm(op["id"])
    assert env.client.post(f"/modifications/{op['id']}/confirm").status_code == 409


def test_stale_preview_is_rejected_whole(env):
    album = env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    item = album.items()[0]
    item.genres = ["Changed"]
    item.store()

    response = env.client.post(f"/modifications/{op['id']}/confirm")

    assert response.status_code == 409
    assert env.client.get(f"/modifications/{op['id']}").json()["status"] == "rejected"
    assert {i.comments for i in fresh_album(env, album).items()} == {""}


def test_mtime_change_is_not_stale(env):
    album = env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    item = album.items()[0]
    item.mtime = 12345
    item.store()
    assert env.confirm(op["id"])["status"] == "applied"


def test_failure_carries_on_and_reports_per_object(env):
    album = env.seed_album()
    items = album.items()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    os.remove(items[0].path)

    done = env.confirm(op["id"])

    assert done["status"] == "partial"
    assert [f["id"] for f in done["result"]["failed"]] == [items[0].id]
    assert [a["id"] for a in done["result"]["applied"]] == [items[1].id]
    assert MediaFile(env.lib.get_item(items[1].id).path).comments == "x"
    assert env.lib.get_item(items[0].id).comments == "x"


def test_preview_survives_restart(env):
    album = env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    restart(env)
    assert env.confirm(op["id"])["status"] == "applied"
    assert {i.comments for i in fresh_album(env, album).items()} == {"x"}


def test_applying_becomes_interrupted_on_restart(env):
    env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    from beets_api.store import Store
    from beets_api.library import default_store_path

    Store(default_store_path()).transition(op["id"], "awaiting_confirmation", "applying")
    restart(env)
    assert env.client.get(f"/modifications/{op['id']}").json()["status"] == "interrupted"


def test_history_path_comes_from_env_var(env, monkeypatch, tmp_path):
    env.seed_album()
    path = tmp_path / "custom.db"
    monkeypatch.setenv("BEETS_API_DB", str(path))
    restart(env)
    env.preview(scope="item", query="", set={"comments": "x"})
    assert path.exists()


def list_modifications(env, **params):
    response = env.client.get("/modifications", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_modifications_newest_first_with_paging(env):
    env.seed_album()
    ids = [env.preview(scope="item", query="", set={"comments": str(n)})["id"] for n in range(3)]

    body = list_modifications(env)
    assert body["total"] == 3
    assert [m["id"] for m in body["results"]] == ids[::-1]

    page = list_modifications(env, limit=1, offset=1)
    assert (page["total"], page["limit"], page["offset"]) == (3, 1, 1)
    assert [m["id"] for m in page["results"]] == [ids[1]]


def test_list_modifications_summarises_without_the_preview(env):
    env.seed_album()
    op = env.preview(scope="album", query="", set={"albumdisambig": "x"})
    env.confirm(op["id"])

    [summary] = list_modifications(env)["results"]
    assert set(summary) == {"id", "status", "request", "created_at", "updated_at", "counts"}
    assert summary["status"] == "applied"
    assert summary["request"]["set"] == {"albumdisambig": "x"}
    assert summary["counts"] == {"albums": 1, "items": 2, "applied": 3, "failed": 0}


def test_list_modifications_filters_by_status(env):
    env.seed_album()
    waiting = env.preview(scope="item", query="", set={"comments": "a"})["id"]
    nothing = env.preview(scope="item", query="nomatch", set={"comments": "a"})["id"]
    applied = env.preview(scope="item", query="", set={"comments": "b"})["id"]
    env.confirm(applied)

    def ids(*statuses):
        return [m["id"] for m in list_modifications(env, status=list(statuses))["results"]]

    assert ids("awaiting_confirmation") == [waiting]
    assert ids("no_changes", "applied") == [applied, nothing]
    assert list_modifications(env, status="no_such_status")["total"] == 0


def test_list_modifications_excludes_imports(env):
    (env.tmp_path / "empty").mkdir()
    response = env.client.post("/imports", json={"paths": [str(env.tmp_path / "empty")]})
    assert response.status_code == 202, response.text
    env.wait(f"/imports/{response.json()['id']}", "completed", "failed")
    assert list_modifications(env) == {"total": 0, "limit": 50, "offset": 0, "results": []}


def test_artpath_cannot_be_modified(env):
    # Otherwise GET /albums/{id}/art would serve any file the server can read.
    env.seed_album()
    for body in ({"set": {"artpath": "/etc/hosts"}}, {"delete": ["artpath"]}):
        response = env.client.post("/modifications", json={"scope": "album", "query": "", **body})
        assert response.status_code == 422
        assert "artpath" in response.json()["detail"]
