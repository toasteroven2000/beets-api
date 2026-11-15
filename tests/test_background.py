"""Nothing that can take long happens inside a request: it returns, and the status is polled."""

import threading

import pytest

from beets_api import modify, removals
from beets_api.store import Progress
from beets_api.undo import Undo

from test_imports import imported, source, start  # noqa: F401  (source is a fixture)


@pytest.fixture
def gate():
    """Holds a background step until the test lets it go."""
    event = threading.Event()
    yield event
    event.set()  # never leave a worker blocked if the test failed


def slow(module, gate):
    real = module._build_preview

    def build(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    return build


@pytest.mark.parametrize("module,path,body", [
    (modify, "/modifications", {"scope": "item", "query": "", "set": {"comments": "x"}}),
    (removals, "/removals", {"scope": "item", "query": "title:One"}),
])
def test_a_preview_returns_at_once_and_is_built_in_the_background(env, monkeypatch, gate, module, path, body):
    env.seed_album()
    monkeypatch.setattr(module, "_build_preview", slow(module, gate))

    response = env.client.post(path, json=body)

    assert response.status_code == 202
    created = response.json()
    assert created["status"] == "previewing" and "preview" not in created["result"]
    assert env.client.get(path).json()["results"][0]["counts"]["items"] == 0  # listed while building
    assert env.client.post(f"{path}/{created['id']}/confirm").status_code == 409  # nothing to confirm yet

    gate.set()
    built = env.built(f"{path}/{created['id']}")
    assert built["status"] == "awaiting_confirmation" and built["result"]["preview"]["items"]


@pytest.mark.parametrize("module,path,body", [
    (modify, "/modifications", {"scope": "item", "query": "", "set": {"comments": "x"}}),
    (removals, "/removals", {"scope": "item", "query": "title:One"}),
])
def test_a_preview_that_breaks_is_reported_as_failed(env, monkeypatch, module, path, body):
    env.seed_album()

    def broken(*_, **__):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(module, "_build_preview", broken)
    created = env.client.post(path, json=body).json()
    failed = env.wait(f"{path}/{created['id']}", "failed")
    assert failed["result"]["error"] == "disk on fire"
    assert env.client.post(f"{path}/{created['id']}/confirm").status_code == 409


def test_confirm_returns_before_any_work_is_done(env, monkeypatch, gate):
    env.seed_album()
    op = env.preview(scope="item", query="", set={"comments": "x"})
    real = modify._apply_item

    def held(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(modify, "_apply_item", held)
    response = env.client.post(f"/modifications/{op['id']}/confirm")
    assert response.status_code == 202 and response.json()["status"] == "queued"
    gate.set()
    assert env.wait(f"/modifications/{op['id']}", "applied")["result"]["failed"] == []


def test_undo_runs_in_the_background_and_blocks_a_new_import_meanwhile(env, source, monkeypatch, gate):  # noqa: F811
    id_ = imported(env, source)
    real = Undo.run

    def held(self, *args, **kwargs):
        gate.wait(10)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Undo, "run", held)
    response = env.client.post(f"/imports/{id_}/undo")

    assert response.status_code == 202 and response.json()["undoing"] is True
    assert len(env.lib.items()) == 2  # nothing has been removed yet
    assert env.client.get(f"/imports/{id_}").json()["undoing"] is True
    refused = env.client.post("/imports", json={"paths": [source]})
    assert refused.status_code == 409 and "undo" in refused.json()["detail"]
    assert env.client.post(f"/imports/{id_}/undo").status_code == 409

    gate.set()
    for _ in range(500):
        done = env.client.get(f"/imports/{id_}").json()
        if not done["undoing"]:
            break
        threading.Event().wait(0.02)
    assert done["result"]["undone"] and len(env.lib.items()) == 0
    start(env, source)  # a new Import is allowed again
    env.client.delete("/imports/2")


def test_a_failed_undo_says_so_and_frees_the_lock(env, source, monkeypatch):  # noqa: F811
    id_ = imported(env, source)

    def broken(self, *args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(Undo, "run", broken)
    assert env.client.post(f"/imports/{id_}/undo").status_code == 202
    for _ in range(500):
        op = env.client.get(f"/imports/{id_}").json()
        if not op["undoing"]:
            break
        threading.Event().wait(0.02)
    assert op["result"]["undo_error"] == "disk on fire" and "undone" not in op["result"]
    assert len(env.lib.items()) == 2


class CountingStore:
    def __init__(self):
        self.saves = []

    def update(self, id_, status, result):
        self.saves.append((status, dict(result)))


def test_progress_saves_at_most_once_per_interval_but_always_when_forced(monkeypatch):
    clock = iter([0.0, 0.1, 0.2, 1.1, 1.2, 1.3])
    monkeypatch.setattr("beets_api.store.time.monotonic", lambda: next(clock))
    store = CountingStore()
    progress = Progress(store, 1, interval=1.0)
    for n in range(4):
        progress.save("applying", {"n": n})  # clock: 0.0 saves, 0.1 and 0.2 skip, 1.1 saves
    progress.save("applied", {"n": 99}, force=True)
    assert [r["n"] for _, r in store.saves] == [0, 3, 99]
