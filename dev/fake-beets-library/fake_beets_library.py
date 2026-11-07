#!/usr/bin/env python3
"""Generate a fake beets library from real MusicBrainz release metadata.

Given a YAML config listing MusicBrainz release MBIDs, fetches the real
artist/album/track titles from the MusicBrainz API, synthesizes a silent,
correctly tagged MP3 for each track, and imports the result into a fresh
beets library rooted at an output directory.

Usage:
    fake_beets_library.py --output-dir OUTPUT_DIR --config CONFIG.yaml

Config format:
    releases:
      - 24f1766e-9635-4d58-a4d4-9413f9f98a4c
      - b84ee12a-09ef-421b-82de-0441a89bming

Produces:
    OUTPUT_DIR/library.db   -- beets SQLite database
    OUTPUT_DIR/Music/       -- imported (renamed/organized by beets) audio files
    OUTPUT_DIR/config.yaml  -- beets config pointing at the above (usable with `beet --config`)
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import musicbrainzngs
import yaml
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

# MusicBrainz asks for "Application-Version (contact-info)"; see
# https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting#Provide_meaningful_User-Agent_strings
USER_AGENT_APP = "audio-sink-fake-beets-library"
USER_AGENT_VERSION = "0.1"

MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 5


def fetch_release(mbid: str) -> dict:
    result = fetch_release_with_retry(mbid)
    return parse_release(mbid, result["release"])


def fetch_release_with_retry(mbid: str) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return musicbrainzngs.get_release_by_id(
                mbid,
                includes=["recordings", "artist-credits", "media", "release-groups"],
            )
        except musicbrainzngs.NetworkError as exc:
            if attempt == MAX_RETRIES:
                raise
            delay = RETRY_BACKOFF_SECONDS * attempt
            print(
                f"  MusicBrainz request failed ({exc}), "
                f"retrying in {delay}s (attempt {attempt}/{MAX_RETRIES}) ...",
                file=sys.stderr,
            )
            time.sleep(delay)


def parse_release(mbid: str, release: dict) -> dict:
    artist = release["artist-credit-phrase"]
    artist_id = release["artist-credit"][0]["artist"]["id"]
    album = release["title"]
    release_group_id = release["release-group"]["id"]
    date = release.get("date", "")
    year = date.split("-")[0] if date else ""

    tracks = []
    for medium in release.get("medium-list", []):
        for track in medium.get("track-list", []):
            tracks.append(
                {
                    "position": int(track["position"]),
                    "title": track["recording"]["title"],
                    "release_track_id": track["id"],
                    "recording_id": track["recording"]["id"],
                }
            )

    if not tracks:
        raise ValueError(f"release {mbid} has no tracks in its medium-list")

    return {
        "mbid": mbid,
        "artist": artist,
        "artist_id": artist_id,
        "album": album,
        "release_group_id": release_group_id,
        "year": year,
        "tracks": tracks,
    }


def make_silent_mp3(dest: Path, duration_seconds: int = 2) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=stereo",
            "-t",
            str(duration_seconds),
            "-q:a",
            "9",
            str(dest),
        ],
        check=True,
    )


def tag_mp3(path: Path, release: dict, track: dict) -> None:
    try:
        tags = EasyID3(path)
    except Exception:
        MP3(path).add_tags()
        tags = EasyID3(path)

    tags["artist"] = release["artist"]
    tags["album"] = release["album"]
    tags["title"] = track["title"]
    tags["tracknumber"] = str(track["position"])
    if release["year"]:
        tags["date"] = release["year"]

    tags["musicbrainz_albumid"] = release["mbid"]
    tags["musicbrainz_albumartistid"] = release["artist_id"]
    tags["musicbrainz_artistid"] = release["artist_id"]
    tags["musicbrainz_releasegroupid"] = release["release_group_id"]
    tags["musicbrainz_trackid"] = track["recording_id"]
    tags["musicbrainz_releasetrackid"] = track["release_track_id"]

    tags.save()


def generate_release_files(release: dict, staging_dir: Path) -> list[Path]:
    album_dir = staging_dir / f"{release['artist']} - {release['album']}"
    album_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for track in release["tracks"]:
        filename = f"{track['position']:02d} - {track['title']}.mp3"
        path = album_dir / filename
        make_silent_mp3(path)
        tag_mp3(path, release, track)
        paths.append(path)
    return paths


def write_beets_config(config_path: Path, library_db: Path, music_dir: Path) -> None:
    config_path.write_text(
        f"""\
directory: {music_dir}
library: {library_db}
import:
  copy: yes
  autotag: no
  resume: no
  incremental: no
plugins:
  - info
  - web
web:
  host: 127.0.0.1
  port: 8337
"""
    )


def import_staged_files(beets_config: Path, staging_dir: Path) -> None:
    subprocess.run(
        [
            "beet",
            "--config",
            str(beets_config),
            "import",
            "-A",
            "-q",
            str(staging_dir),
        ],
        check=True,
    )


def load_config(config_path: Path) -> dict:
    with config_path.open() as f:
        config = yaml.safe_load(f)

    releases = config.get("releases") if config else None
    if not releases:
        raise ValueError(f"config {config_path} has no non-empty 'releases' list")

    return {"releases": releases}


def ensure_empty_output_dir(output_dir: Path) -> None:
    if not output_dir.exists() or not any(output_dir.iterdir()):
        return

    answer = input(f"{output_dir} is not empty. Recreate it? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborting.", file=sys.stderr)
        sys.exit(1)

    shutil.rmtree(output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Directory to hold library.db and Music/"
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="YAML config with a 'releases' list of MBIDs"
    )
    args = parser.parse_args()

    contact = subprocess.run(
        ["git", "config", "user.email"], capture_output=True, text=True, check=True
    ).stdout.strip()
    musicbrainzngs.set_useragent(USER_AGENT_APP, USER_AGENT_VERSION, contact=contact)

    config = load_config(args.config)

    output_dir: Path = args.output_dir.resolve()
    ensure_empty_output_dir(output_dir)

    music_dir = "Music"
    library_db = "library.db"
    music_dir.mkdir(parents=True, exist_ok=True)

    beets_config = output_dir / "config.yaml"
    write_beets_config(beets_config, library_db, music_dir)

    with tempfile.TemporaryDirectory(prefix="fake-beets-staging-") as staging:
        staging_dir = Path(staging)
        for mbid in config["releases"]:
            print(f"Fetching MusicBrainz release {mbid} ...", file=sys.stderr)
            release = fetch_release(mbid)
            print(
                f"  -> {release['artist']} - {release['album']} "
                f"({len(release['tracks'])} tracks)",
                file=sys.stderr,
            )
            generate_release_files(release, staging_dir)

        print(f"Importing into beets library at {library_db} ...", file=sys.stderr)
        import_staged_files(beets_config, staging_dir)

    print(f"Done. Library: {library_db}  Music: {music_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
