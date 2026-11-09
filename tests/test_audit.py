import sqlite3

import pytest

from conftest import make_album_files
from test_imports import choose, finish, prompt, start


def audit(env, **params):
    response = env.client.get("/audit", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def source(env):
    make_album_files(str(env.tmp_path / "incoming" / "a"))
    return str(env.tmp_path / "incoming")


# Modifications


def test_confirmed_modification_audits_each_changed_object(env):
    album = env.seed_album(albumdisambig="old")
    op = env.preview(scope="album", query="", set={"albumdisambig": "new"})
    assert audit(env)["total"] == 0  # a Preview changes nothing

    response = env.client.post(f"/modifications/{op['id']}/confirm", headers={"X-Client": "ui"})
    assert response.status_code == 202
    env.wait(f"/modifications/{op['id']}", "applied")

    entries = audit(env, operation_id=op["id"])["results"]
    item_ids = sorted(i.id for i in album.items())
    assert sorted(e["target"] for e in entries) == [
        f"album:{album.id}", *(f"item:{i}" for i in item_ids)
    ]
    assert {e["client"] for e in entries} == {"ui"}
    [album_entry] = [e for e in entries if e["action"] == "album.modify"]
    assert album_entry["changes"]["albumdisambig"] == {"old": "old", "new": "new"}
    assert {e["action"] for e in entries} == {"album.modify", "item.modify"}


def test_rejected_modification_is_not_audited(env):
    album = env.seed_album()
    op = env.preview(scope="album", query="", set={"albumdisambig": "x"})
    album.albumdisambig = "changed meanwhile"
    album.store()
    assert env.client.post(f"/modifications/{op['id']}/confirm").status_code == 409
    assert audit(env)["total"] == 0


# Imports


def test_import_audits_each_imported_album(env, source):
    response = env.client.post("/imports", json={"paths": [source]}, headers={"X-Client": "ui"})
    id_ = response.json()["id"]
    choose(env, id_, "asis")
    assert finish(env, id_)["status"] == "completed"

    [entry] = audit(env)["results"]
    [album] = env.lib.albums()
    assert (entry["action"], entry["target"]) == ("album.import", f"album:{album.id}")
    assert (entry["client"], entry["operation_id"]) == ("ui", id_)
    assert entry["changes"]["match"] == "asis"
    assert sorted(i["id"] for i in entry["changes"]["items"]) == sorted(i.id for i in album.items())


def test_singleton_import_audits_each_item(env, source):
    id_ = start(env, source, singletons=True)
    choose(env, id_, "asis")
    env.wait(f"/imports/{id_}", "waiting_for_choice", "completed")
    choose(env, id_, "asis")
    finish(env, id_)
    entries = audit(env)["results"]
    assert [e["action"] for e in entries] == ["item.import", "item.import"]
    assert {e["target"] for e in entries} == {f"item:{i.id}" for i in env.lib.items()}


def test_skipped_import_is_not_audited(env, source):
    id_ = start(env, source)
    choose(env, id_, "skip")
    finish(env, id_)
    assert audit(env)["total"] == 0


def test_removing_a_duplicate_on_import_is_audited(env, source):
    old = env.seed_album()
    id_ = start(env, source)
    choose(env, id_, "asis")
    env.wait(f"/imports/{id_}", "waiting_for_choice")
    assert prompt(env, id_)["kind"] == "duplicate"
    choose(env, id_, "remove")
    assert finish(env, id_)["status"] == "completed"

    [new] = [a for a in env.lib.albums() if a.id != old.id]
    removed = audit(env, action="album.remove")["results"]
    assert [e["target"] for e in removed] == [f"album:{old.id}"]
    assert removed[0]["changes"]["replaced_by"] == f"album:{new.id}"
    assert removed[0]["changes"]["removed"]["album"] == "My Album"


# The audit itself


def test_audit_filters_and_pages_newest_first(env):
    first, second = env.seed_album("First"), env.seed_album("Second")
    jpeg = b"\xff\xd8\xff\xe0jpeg"
    for album in (first, second, first):
        env.client.put(f"/albums/{album.id}/art", content=jpeg)
    env.client.delete(f"/albums/{second.id}/art")

    everything = audit(env)
    assert everything["total"] == 4
    ids = [e["id"] for e in everything["results"]]
    assert ids == sorted(ids, reverse=True)
    assert audit(env, target=f"album:{first.id}")["total"] == 2
    assert audit(env, action=["art.remove"])["total"] == 1
    assert audit(env, action=["art.remove", "art.upload"])["total"] == 4
    page = audit(env, limit=1, offset=1)
    assert [e["id"] for e in page["results"]] == [ids[1]]


def test_audit_is_append_only(env):
    album = env.seed_album()
    env.client.put(f"/albums/{album.id}/art", content=b"\xff\xd8\xff\xe0jpeg")
    with sqlite3.connect(env.client.app.state.store.path) as conn:
        for sql in ("UPDATE audit SET client = 'forged'", "DELETE FROM audit"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
    assert audit(env)["results"][0]["client"] is None
