import os
import time

from conftest import restart


def removal(env, album):
    """Remove an Album through the API, returning the Removal's id."""
    op = env.client.post("/removals", json={"scope": "album", "query": f"id:{album.id}"}).json()
    env.built(f"/removals/{op['id']}")
    env.client.post(f"/removals/{op['id']}/confirm")
    env.wait(f"/removals/{op['id']}", "applied")
    return op["id"]


def preview(env, **body):
    response = env.client.post("/purges", json=body)
    assert response.status_code == 202, response.text
    return env.built(f"/purges/{response.json()['id']}")


def confirm(env, id_, **headers):
    response = env.client.post(f"/purges/{id_}/confirm", headers=headers)
    assert response.status_code == 202, response.text
    return env.wait(f"/purges/{id_}", "applied", "partial", "rejected")


def test_the_trash_lists_a_folder_per_operation(env):
    first = removal(env, env.seed_album("One", titles=("A", "B")))
    second = removal(env, env.seed_album("Two", titles=("C",)))
    listing = env.client.get("/trash").json()
    assert listing["configured"] is True
    assert [(f["name"], f["files"], f["operation_id"], f["actions"]) for f in listing["folders"]] == [
        (str(first), 2, first, ["album.remove"]), (str(second), 1, second, ["album.remove"])]


def test_purge_previews_then_deletes_the_folders_for_good(env):
    first = removal(env, env.seed_album("One"))
    second = removal(env, env.seed_album("Two"))

    op = preview(env, folders=[str(first)])

    assert op["status"] == "awaiting_confirmation"
    assert [f["name"] for f in op["result"]["preview"]["folders"]] == [str(first)]
    assert (env.trash / str(first)).exists()  # a Preview deletes nothing

    done = confirm(env, op["id"], **{"X-Beets-API-Client": "ui", "X-Beets-API-Actor": "dana"})

    assert done["status"] == "applied" and done["result"]["purged"] == [str(first)]
    assert not (env.trash / str(first)).exists() and (env.trash / str(second)).exists()
    (entry,) = env.client.get("/audit", params={"action": "trash.purge"}).json()["results"]
    assert (entry["target"], entry["actor"], entry["operation_id"]) == (
        f"trash:{first}", "dana", op["id"])
    assert entry["changes"]["files"] == 2


def test_no_filters_means_every_folder(env):
    removal(env, env.seed_album("One"))
    removal(env, env.seed_album("Two"))
    op = preview(env)
    assert len(op["result"]["preview"]["folders"]) == 2
    assert confirm(env, op["id"])["status"] == "applied"
    assert env.client.get("/trash").json()["folders"] == []


def test_older_than_keeps_newer_folders(env):
    old = removal(env, env.seed_album("One"))
    old_dir = env.trash / str(old)
    longago = time.time() - 86400 * 40
    for path in [old_dir, *old_dir.rglob("*")]:
        os.utime(path, (longago, longago))
    new = removal(env, env.seed_album("Two"))
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400 * 30))
    op = preview(env, older_than=cutoff)
    assert [f["name"] for f in op["result"]["preview"]["folders"]] == [str(old)]
    assert (env.trash / str(new)).exists()


def test_unknown_folders_are_refused(env):
    removal(env, env.seed_album())
    response = env.client.post("/purges", json={"folders": ["../music"]})
    assert response.status_code == 422


def test_nothing_to_purge_is_no_changes(env):
    assert preview(env)["status"] == "no_changes"


def test_confirm_is_rejected_when_the_folder_changed_after_the_preview(env):
    first = removal(env, env.seed_album("One"))
    op = preview(env)
    (env.trash / str(first) / "extra.txt").write_text("added later")
    assert confirm(env, op["id"])["status"] == "rejected"
    assert (env.trash / str(first)).exists()


def test_history_lists_purges_without_their_preview(env):
    removal(env, env.seed_album())
    op = preview(env)
    page = env.client.get("/purges").json()
    assert page["total"] == 1 and page["results"][0]["id"] == op["id"]
    assert page["results"][0]["counts"]["folders"] == 1 and "result" not in page["results"][0]


def test_confirm_is_refused_when_the_trash_is_not_configured(env, monkeypatch):
    removal(env, env.seed_album())
    op = preview(env)
    monkeypatch.delenv("BEETS_API_TRASH_DIR")
    restart(env)
    assert env.client.get("/trash").json() == {"configured": False, "folders": []}
    assert preview(env)["status"] == "no_changes"
    assert env.client.post(f"/purges/{op['id']}/confirm").status_code == 409
    assert any(env.trash.iterdir())  # nothing was deleted
