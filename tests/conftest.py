import os
import shutil
import time

import pytest
from beets.library import Item
from fastapi.testclient import TestClient
from mutagen.easyid3 import EasyID3

from beets_api.app import create_app

MP3_FRAME = b"\xff\xfb\x90\x00" + b"\0" * 413


def make_mp3(path, artist, album, title, track):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(MP3_FRAME * 40)
    tags = EasyID3()
    tags["artist"], tags["album"], tags["title"] = artist, album, title
    tags["tracknumber"] = str(track)
    tags.save(path)
    return path


def make_album_files(directory, artist="Artist", album="My Album", titles=("One", "Two")):
    return [
        make_mp3(os.path.join(directory, f"{n} {t}.mp3"), artist, album, t, n)
        for n, t in enumerate(titles, 1)
    ]


class Env:
    def __init__(self, tmp_path, client):
        self.tmp_path = tmp_path
        self.client = client

    @property
    def lib(self):
        return self.client.app.state.lib

    def seed_album(self, name="My Album", artist="Artist", titles=("One", "Two"), **album_fields):
        folder = self.tmp_path / "seed" / f"{artist}-{name}"
        files = make_album_files(str(folder), artist, name, titles)
        items = [Item.from_path(f) for f in files]
        for item in items:
            item.albumartist = artist
        album = self.lib.add_album(items)
        for key, value in album_fields.items():
            album[key] = value
        album.store()
        for item in album.items():
            item.move()
            item.store()
        return album

    def preview(self, **body):
        response = self.client.post("/modifications", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def wait(self, url, *statuses, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = self.client.get(url).json()
            if op["status"] in statuses:
                return op
            time.sleep(0.02)
        raise AssertionError(f"{url} stuck: {op}")

    def confirm(self, op_id):
        response = self.client.post(f"/modifications/{op_id}/confirm")
        assert response.status_code == 202, response.text
        return self.wait(f"/modifications/{op_id}", "applied", "partial", "rejected")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "directory: Music\nlibrary: library.db\nplugins: []\n"
        "import:\n  copy: yes\n  autotag: no\n  resume: no\n"
    )
    monkeypatch.setenv("BEETSDIR", str(home))
    monkeypatch.delenv("BEETS_API_DB", raising=False)
    monkeypatch.delenv("BEETS_API_IMPORT_PATHS", raising=False)
    with TestClient(create_app(), base_url="http://localhost") as client:
        yield Env(tmp_path, client)


def restart(env):
    """Simulate a server restart on the same BEETSDIR."""
    env.client = TestClient(create_app(), base_url="http://localhost")
