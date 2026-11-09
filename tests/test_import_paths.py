import os

import pytest

from conftest import restart


def configure(env, monkeypatch, *roots):
    monkeypatch.setenv("BEETS_API_IMPORT_PATHS", os.pathsep.join(str(r) for r in roots))
    restart(env)


def browse(env, path=None, status=200):
    params = {} if path is None else {"path": str(path)}
    response = env.client.get("/import-paths", params=params)
    assert response.status_code == status, response.text
    return response.json()


def names(listing):
    return [e["name"] for e in listing["entries"]]


@pytest.fixture
def root(env):
    root = env.tmp_path.resolve() / "incoming"
    (root / "Album").mkdir(parents=True)
    (root / "Album" / "01.mp3").write_bytes(b"x" * 5)
    (root / "Album" / "cover.jpg").write_bytes(b"")
    (root / "loose.flac").write_bytes(b"")
    return root


def test_unset_import_paths_list_nothing_and_refuse_browsing(env, root):
    assert browse(env) == {"path": None, "parent": None, "entries": []}
    browse(env, root, status=403)


def test_roots_are_listed_and_missing_ones_hidden(env, root, monkeypatch):
    configure(env, monkeypatch, root, env.tmp_path / "unmounted")
    listing = browse(env)
    assert listing["path"] is None
    assert [e["path"] for e in listing["entries"]] == [str(root)]
    assert listing["entries"][0]["type"] == "dir"


def test_a_directory_lists_its_children_directories_first(env, root, monkeypatch):
    (root / "b.mp3").write_bytes(b"")
    (root / "A.mp3").write_bytes(b"")
    (root / "zeta").mkdir()
    configure(env, monkeypatch, root)
    listing = browse(env, root)
    assert listing["path"] == str(root)
    assert listing["parent"] is None
    assert names(listing) == ["Album", "zeta", "A.mp3", "b.mp3", "loose.flac"]


def test_entries_carry_path_type_size_and_mtime(env, root, monkeypatch):
    configure(env, monkeypatch, root)
    listing = browse(env, root / "Album")
    assert listing["parent"] == str(root)
    track = next(e for e in listing["entries"] if e["name"] == "01.mp3")
    assert track["path"] == str(root / "Album" / "01.mp3")
    assert track["type"] == "file"
    assert track["size"] == 5
    assert track["mtime"].endswith("+00:00")
    album = browse(env, root)["entries"][0]
    assert album["type"] == "dir" and "size" not in album


def test_listed_paths_can_be_imported(env, root, monkeypatch):
    configure(env, monkeypatch, root)
    path = browse(env, root)["entries"][0]["path"]
    response = env.client.post("/imports", json={"paths": [path]})
    assert response.status_code == 202, response.text
    env.client.delete(f"/imports/{response.json()['id']}")
    env.wait(f"/imports/{response.json()['id']}", "completed", "failed", "aborted")


def test_hidden_entries_are_left_out(env, root, monkeypatch):
    (root / ".DS_Store").write_bytes(b"")
    (root / ".partial").mkdir()
    configure(env, monkeypatch, root)
    assert names(browse(env, root)) == ["Album", "loose.flac"]


def test_symlinks_leaving_the_roots_are_hidden(env, root, monkeypatch):
    outside = env.tmp_path.resolve() / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    (root / "broken").symlink_to(root / "nowhere")
    configure(env, monkeypatch, root)
    assert names(browse(env, root)) == ["Album", "loose.flac"]


def test_symlinks_inside_the_roots_keep_their_own_path(env, root, monkeypatch):
    (root / "shortcut").symlink_to(root / "Album")
    configure(env, monkeypatch, root)
    link = next(e for e in browse(env, root)["entries"] if e["name"] == "shortcut")
    assert link == {**link, "path": str(root / "shortcut"), "type": "dir"}
    listing = browse(env, root / "shortcut")
    assert listing["parent"] == str(root)
    assert names(listing) == ["01.mp3", "cover.jpg"]


@pytest.mark.parametrize(
    "path, status",
    [
        ("relative/path", 400),
        ("{tmp}", 403),
        ("{root}/../outside", 403),
        ("{root}/missing", 404),
        ("{root}/loose.flac", 400),
    ],
)
def test_bad_paths_are_refused(env, root, monkeypatch, path, status):
    configure(env, monkeypatch, root)
    browse(env, path.format(tmp=env.tmp_path, root=root), status=status)


def test_an_unreadable_directory_is_refused(env, root, monkeypatch):
    locked = root / "locked"
    locked.mkdir(mode=0)
    configure(env, monkeypatch, root)
    try:
        browse(env, locked, status=403)
    finally:
        locked.chmod(0o755)
