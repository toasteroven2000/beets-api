import os
import time

import pytest
from beets import metadata_plugins
from beets.autotag import AlbumInfo, TrackInfo

from conftest import make_album_files, restart


def album_info(album="Right Album", artist="Right Artist", titles=("One", "Two"), id_="alb-1"):
    return AlbumInfo(
        album=album,
        album_id=id_,
        artist=artist,
        artist_id="art-1",
        data_source="fake",
        tracks=[
            TrackInfo(title=t, track_id=f"trk-{n}", index=n, medium=1,
                      medium_index=n, medium_total=len(titles), length=2.0)
            for n, t in enumerate(titles, 1)
        ],
    )


@pytest.fixture
def source(env):
    folder = env.tmp_path / "incoming" / "a"
    make_album_files(str(folder))
    return str(env.tmp_path / "incoming")


def start(env, *paths, **options):
    response = env.client.post("/imports", json={"paths": list(paths), **options})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def prompt(env, id_):
    return env.wait(f"/imports/{id_}", "waiting_for_choice", "completed", "failed")["result"]["prompt"]


def choose(env, id_, action, **details):
    current = prompt(env, id_)
    response = env.client.post(
        f"/imports/{id_}/choice", json={"prompt_id": current["id"], "action": action, **details}
    )
    assert response.status_code == 202, response.text


def finish(env, id_):
    return env.wait(f"/imports/{id_}", "completed", "failed", "aborted")


def test_prompts_even_though_config_disables_autotag(env, source):
    id_ = start(env, source)
    current = prompt(env, id_)
    assert current["kind"] == "album"
    assert current["choices"] == ["asis", "skip", "tracks", "albums", "search", "abort"]
    assert len(env.lib.items()) == 0

    choose(env, id_, "asis")
    done = finish(env, id_)

    assert done["status"] == "completed"
    assert len(env.lib.items()) == 2
    assert len(done["result"]["outcomes"][0]["items"]) == 2
    assert done["result"]["choices"][0]["action"] == "asis"


def test_apply_candidate_shows_changes_then_applies(env, source, monkeypatch):
    monkeypatch.setattr(metadata_plugins, "candidates", lambda *a, **k: iter([album_info()]))
    id_ = start(env, source)
    current = prompt(env, id_)
    candidate = current["candidates"][0]
    assert candidate["album"] == "Right Album"
    changes = candidate["tracks"][0]["changes"]
    assert changes["album"] == {"old": "My Album", "new": "Right Album"}

    choose(env, id_, "apply", candidate=0)
    assert finish(env, id_)["status"] == "completed"
    assert {i.album for i in env.lib.items()} == {"Right Album"}
    assert all(os.path.exists(i.path) for i in env.lib.items())


def test_strong_match_still_pauses(env, source, monkeypatch):
    info = album_info(album="My Album", artist="Artist")
    monkeypatch.setattr(metadata_plugins, "candidates", lambda *a, **k: iter([info]))
    id_ = start(env, source)
    current = prompt(env, id_)
    assert current["recommendation"] == "strong"
    assert len(env.lib.items()) == 0
    env.client.delete(f"/imports/{id_}")


def test_search_replaces_candidates_and_prompts_again(env, source, monkeypatch):
    monkeypatch.setattr(metadata_plugins, "candidates", lambda *a, **k: iter(()))
    id_ = start(env, source)
    first = prompt(env, id_)
    assert first["candidates"] == []

    monkeypatch.setattr(metadata_plugins, "candidates", lambda *a, **k: iter([album_info()]))
    choose(env, id_, "search", artist="Right Artist", name="Right Album")
    env.wait(f"/imports/{id_}", "waiting_for_choice")
    second = prompt(env, id_)
    assert second["id"] != first["id"]
    assert second["candidates"][0]["album"] == "Right Album"
    choose(env, id_, "skip")
    finish(env, id_)


def test_skip_imports_nothing(env, source):
    id_ = start(env, source)
    choose(env, id_, "skip")
    done = finish(env, id_)
    assert done["result"]["outcomes"][0]["skipped"] is True
    assert len(env.lib.items()) == 0


def test_tracks_choice_prompts_for_each_item(env, source):
    id_ = start(env, source)
    choose(env, id_, "tracks")
    for _ in range(2):
        env.wait(f"/imports/{id_}", "waiting_for_choice")
        current = prompt(env, id_)
        assert current["kind"] == "item"
        choose(env, id_, "asis")
        env.wait(f"/imports/{id_}", "waiting_for_choice", "completed")
    assert finish(env, id_)["status"] == "completed"
    assert len(env.lib.items()) == 2
    assert all(i.album_id is None for i in env.lib.items())


def test_singletons_option(env, source):
    id_ = start(env, source, singletons=True)
    assert prompt(env, id_)["kind"] == "item"
    env.client.delete(f"/imports/{id_}")
    finish(env, id_)


def test_stale_prompt_and_invalid_choices_are_rejected(env, source):
    id_ = start(env, source)
    current = prompt(env, id_)
    post = lambda **b: env.client.post(f"/imports/{id_}/choice", json=b).status_code
    assert post(prompt_id="old", action="skip") == 409
    assert post(prompt_id=current["id"], action="nonsense") == 422
    assert post(prompt_id=current["id"], action="apply", candidate=0) == 422
    assert post(prompt_id=current["id"], action="search") == 422
    assert post(prompt_id=current["id"], action="asis") == 202
    assert post(prompt_id=current["id"], action="asis") == 409
    finish(env, id_)


def test_abort_stops_and_imports_nothing(env, source):
    id_ = start(env, source)
    prompt(env, id_)
    assert env.client.delete(f"/imports/{id_}").status_code == 202
    assert finish(env, id_)["status"] == "aborted"
    assert len(env.lib.items()) == 0
    assert env.client.delete(f"/imports/{id_}").status_code == 409


def test_abort_choice(env, source):
    id_ = start(env, source)
    choose(env, id_, "abort")
    assert finish(env, id_)["status"] == "aborted"


def test_one_active_import_then_next_can_start(env, source):
    id_ = start(env, source)
    assert env.client.post("/imports", json={"paths": [source]}).status_code == 409
    prompt(env, id_)
    env.client.delete(f"/imports/{id_}")
    finish(env, id_)
    next_id = start(env, source)
    assert prompt(env, next_id)["kind"] == "album"
    env.client.delete(f"/imports/{next_id}")
    finish(env, next_id)


def test_modifications_work_while_import_waits_for_choice(env, source):
    env.seed_album("Existing", titles=("Old",))
    id_ = start(env, source)
    prompt(env, id_)
    op = env.preview(scope="item", query="", set={"comments": "x"})
    assert env.confirm(op["id"])["status"] == "applied"
    env.client.delete(f"/imports/{id_}")
    finish(env, id_)


def test_bad_paths_are_rejected(env):
    assert env.client.post("/imports", json={"paths": []}).status_code == 422
    assert env.client.post("/imports", json={"paths": ["/does/not/exist"]}).status_code == 422


@pytest.mark.parametrize(
    "action,expected", [("skip", 2), ("keep", 4), ("remove", 2), ("merge", None)]
)
def test_duplicate_choices(env, source, action, expected):
    env.seed_album()
    id_ = start(env, source)
    choose(env, id_, "asis")
    env.wait(f"/imports/{id_}", "waiting_for_choice")
    current = prompt(env, id_)
    assert current["kind"] == "duplicate"
    assert current["existing"][0]["album"] == "My Album"
    choose(env, id_, action)
    if action == "merge":
        env.wait(f"/imports/{id_}", "waiting_for_choice")
        merged = prompt(env, id_)
        assert merged["kind"] == "album"
        assert sum(p.endswith(".mp3") for p in merged["paths"]) == 2
        assert len(merged["candidates"]) == 0
        env.client.delete(f"/imports/{id_}")
        finish(env, id_)
        return
    assert finish(env, id_)["status"] == "completed"
    assert len(env.lib.items()) == expected


def test_waiting_import_is_interrupted_after_restart_and_reimport_skips_finished(env):
    incoming = env.tmp_path / "incoming"
    make_album_files(str(incoming / "a"), album="Album A")
    make_album_files(str(incoming / "b"), album="Album B")
    id_ = start(env, str(incoming))
    choose(env, id_, "asis")
    env.wait(f"/imports/{id_}", "waiting_for_choice")
    # Wait for album A's files to finish, which records resume progress.
    end = time.monotonic() + 10
    music = env.tmp_path / "home" / "Music" / "Artist" / "Album A"
    while time.monotonic() < end and not (music.exists() and len(list(music.iterdir())) == 2):
        time.sleep(0.02)
    time.sleep(0.3)

    restart(env)

    assert env.client.get(f"/imports/{id_}").json()["status"] == "interrupted"
    second = start(env, str(incoming))
    current = prompt(env, second)
    assert current["current"]["album"] == "Album B"
    choose(env, second, "asis")
    assert finish(env, second)["status"] == "completed"
    assert len(env.lib.items()) == 4


def restart_with_import_paths(env, monkeypatch, value):
    monkeypatch.setenv("BEETS_API_IMPORT_PATHS", value)
    restart(env)


def test_import_paths_allow_paths_inside_a_root(env, source, monkeypatch):
    other = env.tmp_path / "elsewhere"
    other.mkdir()
    restart_with_import_paths(env, monkeypatch, os.pathsep.join([str(other), source]))
    id_ = start(env, os.path.join(source, "a"))
    assert prompt(env, id_)["kind"] == "album"
    choose(env, id_, "abort")
    finish(env, id_)


def test_import_paths_refuse_paths_outside_every_root(env, source, monkeypatch):
    allowed = env.tmp_path / "allowed"
    allowed.mkdir()
    restart_with_import_paths(env, monkeypatch, str(allowed))
    response = env.client.post("/imports", json={"paths": [source]})
    assert response.status_code == 403
    assert source in response.json()["detail"]
    assert env.client.get("/imports/1").status_code == 404


def test_import_paths_refuse_a_symlink_that_leaves_the_root(env, source, monkeypatch):
    allowed = env.tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "link").symlink_to(source)
    restart_with_import_paths(env, monkeypatch, str(allowed))
    response = env.client.post("/imports", json={"paths": [str(allowed / "link")]})
    assert response.status_code == 403


def test_import_paths_refuse_a_sibling_sharing_the_root_prefix(env, monkeypatch):
    allowed = env.tmp_path / "in"
    allowed.mkdir()
    sibling = env.tmp_path / "incoming"
    make_album_files(str(sibling))
    restart_with_import_paths(env, monkeypatch, str(allowed))
    response = env.client.post("/imports", json={"paths": [str(sibling)]})
    assert response.status_code == 403


def test_empty_import_paths_refuse_every_import(env, source, monkeypatch):
    restart_with_import_paths(env, monkeypatch, "")
    response = env.client.post("/imports", json={"paths": [source]})
    assert response.status_code == 403


def list_imports(env, **params):
    response = env.client.get("/imports", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def empty_import(env, name):
    (env.tmp_path / name).mkdir()
    id_ = start(env, str(env.tmp_path / name))
    finish(env, id_)
    return id_


def test_list_imports_newest_first_with_paging(env):
    ids = [empty_import(env, f"empty{n}") for n in range(3)]

    body = list_imports(env)
    assert body["total"] == 3
    assert [i["id"] for i in body["results"]] == ids[::-1]

    page = list_imports(env, limit=1, offset=1)
    assert (page["total"], page["limit"], page["offset"]) == (3, 1, 1)
    assert [i["id"] for i in page["results"]] == [ids[1]]


def test_list_imports_summarises_without_the_prompt(env, source):
    id_ = start(env, source)
    prompt(env, id_)
    [waiting] = list_imports(env)["results"]
    assert set(waiting) == {"id", "status", "request", "created_at", "updated_at", "counts"}
    assert waiting["status"] == "waiting_for_choice"
    assert waiting["counts"] == {"choices": 0, "tasks": 0, "skipped": 0, "items": 0}

    choose(env, id_, "asis")
    assert finish(env, id_)["status"] == "completed"
    [done] = list_imports(env)["results"]
    assert done["request"]["paths"] == [source]
    assert done["counts"] == {"choices": 1, "tasks": 1, "skipped": 0, "items": 2}


def test_list_imports_filters_by_status(env, source):
    completed = empty_import(env, "empty")
    aborted = start(env, source)
    choose(env, aborted, "abort")
    finish(env, aborted)

    def ids(*statuses):
        return [i["id"] for i in list_imports(env, status=list(statuses))["results"]]

    assert ids("aborted") == [aborted]
    assert ids("completed", "aborted") == [aborted, completed]
    assert list_imports(env, status="no_such_status")["total"] == 0


def test_list_imports_excludes_modifications(env):
    env.seed_album()
    env.preview(scope="item", query="", set={"comments": "x"})
    assert list_imports(env) == {"total": 0, "limit": 50, "offset": 0, "results": []}
