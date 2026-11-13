import sqlite3

import pytest

from beets_api.store import Store
from conftest import make_album_files

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


def as_(actor, client="tester"):
    return {"X-Beets-API-Actor": actor, "X-Beets-API-Client": client}


def entries(env, **params):
    return env.client.get("/audit", params=params).json()["results"]


def test_modification_is_attributed_to_who_confirms_it_not_who_previews_it(env):
    env.seed_album()
    created = env.client.post(
        "/modifications", headers=as_("alice"),
        json={"scope": "album", "query": "album:'My Album'", "set": {"genre": "Rock"}, "delete": []},
    ).json()
    env.client.post(f"/modifications/{created['id']}/confirm", headers=as_("bob"))
    env.wait(f"/modifications/{created['id']}", "applied")
    (entry,) = entries(env, action="album.modify")
    assert (entry["actor"], entry["client"]) == ("bob", "tester")


def test_art_changes_record_the_actor(env):
    album = env.seed_album()
    assert env.client.put(f"/albums/{album.id}/art", content=PNG, headers=as_("alice")).status_code == 200
    assert env.client.delete(f"/albums/{album.id}/art", headers=as_("bob")).status_code == 200
    assert {(e["action"], e["actor"]) for e in entries(env)} == {("art.upload", "alice"), ("art.remove", "bob")}


def test_without_headers_there_is_no_actor_or_client(env):
    album = env.seed_album()
    env.client.put(f"/albums/{album.id}/art", content=PNG)
    (entry,) = entries(env)
    assert entry["actor"] is None and entry["client"] is None


def test_the_old_client_header_still_works_and_the_new_one_wins(env):
    album = env.seed_album()
    env.client.put(f"/albums/{album.id}/art", content=PNG, headers={"X-Client": "old"})
    env.client.put(f"/albums/{album.id}/art", content=PNG, headers={"X-Client": "old", "X-Beets-API-Client": "new"})
    assert [e["client"] for e in entries(env)] == ["new", "old"]


def test_actor_is_trimmed_and_refused_when_unsafe(env):
    album = env.seed_album()
    url = f"/albums/{album.id}/art"
    assert env.client.put(url, content=PNG, headers={"X-Beets-API-Actor": "  tim  "}).status_code == 200
    assert entries(env)[0]["actor"] == "tim"
    for bad in ("x" * 201, "ti\tm"):
        refused = env.client.put(url, content=PNG, headers={"X-Beets-API-Actor": bad})
        assert refused.status_code == 400, bad
    assert len(entries(env)) == 1  # a refused request changes nothing
    # An empty header means no actor, not an empty one.
    env.client.put(url, content=PNG, headers={"X-Beets-API-Actor": " "})
    assert entries(env)[0]["actor"] is None


def test_audit_filters_by_actor(env):
    album = env.seed_album()
    env.client.put(f"/albums/{album.id}/art", content=PNG, headers=as_("alice"))
    env.client.delete(f"/albums/{album.id}/art", headers=as_("bob"))
    assert [e["action"] for e in entries(env, actor="alice")] == ["art.upload"]
    assert entries(env, actor="nobody") == []


def test_audit_written_before_actors_gets_the_column_with_old_rows_empty(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, client TEXT,"
            " action TEXT NOT NULL, target TEXT NOT NULL, operation_id INTEGER, changes TEXT NOT NULL);"
            "INSERT INTO audit (at, client, action, target, changes) VALUES ('t', 'c', 'album.modify', 'album:1', '{}');"
        )
    store = Store(path)
    Store(path)  # opening it again is harmless
    store.audit("art.remove", "album:2", {}, "c", actor="tim")
    _, rows = store.audit_page(10, 0)
    assert [(r["target"], r["actor"]) for r in rows] == [("album:2", "tim"), ("album:1", None)]
    with pytest.raises(sqlite3.DatabaseError):  # still append-only
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE audit SET actor = 'x'")


# ---- Imports: each Choice has its own Actor ----

def start(env, *paths):
    response = env.client.post("/imports", json={"paths": list(paths)}, headers=as_("alice"))
    assert response.status_code == 202, response.text
    return response.json()["id"]


def answer(env, id_, actor, action, **details):
    prompt = env.wait(f"/imports/{id_}", "waiting_for_choice")["result"]["prompt"]
    response = env.client.post(
        f"/imports/{id_}/choice", headers=as_(actor),
        json={"prompt_id": prompt["id"], "action": action, **details},
    )
    assert response.status_code == 202, response.text


@pytest.fixture
def source(env):
    make_album_files(str(env.tmp_path / "incoming" / "a"))
    return str(env.tmp_path / "incoming")


def test_each_choice_has_its_actor_and_the_library_change_takes_the_deciding_ones(env, source):
    id_ = start(env, source)
    answer(env, id_, "bob", "asis")
    done = env.wait(f"/imports/{id_}", "completed")
    assert done["result"]["actor"] == "alice"  # who started it
    assert [c["actor"] for c in done["result"]["choices"]] == ["bob"]
    (entry,) = entries(env, operation_id=id_)
    assert (entry["action"], entry["actor"], entry["client"]) == ("album.import", "bob", "tester")


def test_a_searched_import_is_attributed_to_the_final_choice(env, source):
    id_ = start(env, source)
    answer(env, id_, "bob", "search", artist="Artist", name="My Album")
    answer(env, id_, "carol", "asis")
    done = env.wait(f"/imports/{id_}", "completed")
    assert [c["actor"] for c in done["result"]["choices"]] == ["bob", "carol"]
    assert entries(env, operation_id=id_)[0]["actor"] == "carol"


def test_removing_a_duplicate_is_attributed_to_who_chose_remove(env, source):
    first = start(env, source)
    answer(env, first, "bob", "asis")
    env.wait(f"/imports/{first}", "completed")
    second = start(env, source)
    answer(env, second, "bob", "asis")
    answer(env, second, "carol", "remove")
    env.wait(f"/imports/{second}", "completed")
    by_action = {e["action"]: e["actor"] for e in entries(env, operation_id=second)}
    assert by_action == {"album.import": "bob", "album.remove": "carol"}


def test_skipped_choices_are_recorded_on_the_import_but_not_in_the_audit(env, source):
    id_ = start(env, source)
    answer(env, id_, "bob", "skip")
    done = env.wait(f"/imports/{id_}", "completed")
    assert done["result"]["choices"][0]["actor"] == "bob"
    assert entries(env, operation_id=id_) == []


def test_undo_is_attributed_to_who_confirms_it(env, source):
    id_ = start(env, source)
    answer(env, id_, "bob", "asis")
    env.wait(f"/imports/{id_}", "completed")
    assert env.client.post(f"/imports/{id_}/undo", headers=as_("carol")).status_code == 200
    removal = [e for e in entries(env, operation_id=id_) if e["action"] == "album.remove"][0]
    assert removal["actor"] == "carol"
