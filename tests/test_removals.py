import os
import shutil

from conftest import restart



def preview(env, **body):
    response = env.client.post("/removals", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def confirm(env, op_id, **headers):
    response = env.client.post(f"/removals/{op_id}/confirm", headers=headers)
    assert response.status_code == 202, response.text
    return env.wait(f"/removals/{op_id}", "applied", "partial", "rejected")


def audit(env, **params):
    return env.client.get("/audit", params=params).json()["results"]


def test_preview_changes_nothing_then_confirm_removes_records_and_files(env):
    album = env.seed_album()
    paths = [os.fsdecode(i.path) for i in album.items()]

    op = preview(env, scope="album", query='album:"My Album"')

    assert op["status"] == "awaiting_confirmation"
    (entry,) = op["result"]["preview"]["albums"]
    assert [i["path"] for i in entry["items"]] == paths
    assert env.lib.get_album(album.id) is not None
    assert all(os.path.exists(p) for p in paths)

    done = confirm(env, op["id"], **{"X-Beets-API-Client": "ui", "X-Beets-API-Actor": "dana"})

    assert done["status"] == "applied" and done["result"]["failed"] == []
    assert env.lib.get_album(album.id) is None
    assert env.lib.items() is not None and len(env.lib.items()) == 0
    assert not any(os.path.exists(p) for p in paths)
    assert sorted(p.name for p in (env.trash / str(op["id"])).rglob("*.mp3")) == sorted(
        os.path.basename(p) for p in paths)  # moved, not deleted
    (record,) = audit(env, action="album.remove")
    assert (record["target"], record["actor"], record["operation_id"]) == (
        f"album:{album.id}", "dana", op["id"])
    assert sorted(t["from"] for t in record["changes"]["trashed_to"]) == sorted(paths)
    assert record["changes"]["left_in_place"] == []


def test_removing_the_last_item_also_removes_its_album(env):
    album = env.seed_album(titles=("One",))
    (item,) = album.items()
    op = preview(env, scope="item", query=f"id:{item.id}")

    assert confirm(env, op["id"])["status"] == "applied"

    assert env.lib.get_item(item.id) is None and env.lib.get_album(album.id) is None
    assert {e["target"] for e in audit(env, operation_id=op["id"])} == {
        f"item:{item.id}", f"album:{album.id}"}


def test_removing_one_item_keeps_the_album(env):
    album = env.seed_album()
    first = album.items()[0]
    op = preview(env, scope="item", query=f"id:{first.id}")
    assert confirm(env, op["id"])["status"] == "applied"
    assert env.lib.get_album(album.id) is not None and len(album.items()) == 1


def test_a_blank_query_is_refused(env):
    env.seed_album()
    assert env.client.post("/removals", json={"scope": "item", "query": "  "}).status_code == 422


def test_nothing_matching_is_no_changes(env):
    env.seed_album()
    assert preview(env, scope="album", query="album:nope")["status"] == "no_changes"


def test_confirm_is_rejected_if_the_library_changed_after_the_preview(env):
    album = env.seed_album()
    op = preview(env, scope="album", query=f"id:{album.id}")
    changed = env.lib.get_album(album.id)
    changed.albumdisambig = "later"
    changed.store()

    response = env.client.post(f"/removals/{op['id']}/confirm")

    assert response.status_code == 409
    assert env.client.get(f"/removals/{op['id']}").json()["status"] == "rejected"
    assert env.lib.get_album(album.id) is not None


def test_files_outside_the_music_directory_are_left_in_place(env):
    album = env.seed_album()
    outside = env.tmp_path / "elsewhere.mp3"
    item = album.items()[0]
    shutil.copy(item.path, outside)
    moved_from = os.fsdecode(item.path)
    item.path = os.fsencode(outside)
    item.store()
    op = preview(env, scope="album", query=f"id:{album.id}")

    assert confirm(env, op["id"])["status"] == "applied"

    assert outside.exists()
    (record,) = audit(env, action="album.remove")
    assert record["changes"]["left_in_place"] == [str(outside)]
    assert moved_from not in record["changes"]["left_in_place"]


def test_history_lists_removals_without_their_preview(env):
    env.seed_album()
    op = preview(env, scope="album", query="album:My")
    page = env.client.get("/removals").json()
    assert page["total"] == 1 and page["results"][0]["id"] == op["id"]
    assert page["results"][0]["counts"]["albums"] == 1 and "result" not in page["results"][0]


def test_preview_works_but_confirm_is_refused_when_the_trash_is_not_configured(env, monkeypatch):
    album = env.seed_album()
    monkeypatch.delenv("BEETS_API_TRASH_DIR")
    restart(env)
    op = preview(env, scope="album", query=f"id:{album.id}")
    assert op["status"] == "awaiting_confirmation" and op["result"]["trash_configured"] is False
    refused = env.client.post(f"/removals/{op['id']}/confirm")
    assert refused.status_code == 409 and "Trash" in refused.json()["detail"]
    assert env.lib.get_album(album.id) is not None
    assert env.client.get(f"/removals/{op['id']}").json()["status"] == "awaiting_confirmation"
