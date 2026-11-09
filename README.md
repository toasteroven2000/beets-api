# beets-api

A small HTTP API for reading and changing a [beets](https://beets.io) music library, including the interactive import flow. It calls beets in-process, so it uses the same library, config and plugins as the `beet` command.

> [!WARNING]
> **This project is alpha.** Expect breaking changes to endpoints, responses and the history database at any time, with no notice and no migrations.

> [!CAUTION]
> **This project is AI coded and comes with no security guarantees.** Most of the code was written by an AI assistant and has not had a security review. The API has no authentication: anything that can reach it can read your library, change metadata, move files and import music. Only run it on `127.0.0.1` on a machine you trust, and back up your library before pointing it at one you care about.

## Running

Dependencies, including beets itself, come from [devenv](https://devenv.sh):

```sh
devenv shell
serve                # http://127.0.0.1:8338, extra arguments go to uvicorn
devenv up            # or run it as the `web` process
python -m pytest     # tests
```

In the dev shell, `BEETSDIR` points at `db/foobar`, a throwaway library, so your real one in `~/.config/beets` is left alone. Outside devenv, the API uses whichever library `BEETSDIR` or beets' default config points to. Its own history (Modifications, Imports, art uploads) is kept in `beets-api.db` in the beets config directory, or at `BEETS_API_DB`.

Interactive API docs are served at `/docs`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/list?scope=item\|album&query=…` | Items or Albums matching a beets query, paged |
| `GET` | `/items/{id}`, `/albums/{id}` | One Item or Album |
| `GET` | `/albums/{id}/art` | The Album's cover image |
| `PUT` | `/albums/{id}/art` | Upload a JPEG or PNG as `cover.jpg`/`cover.png` (never embedded) |
| `GET` | `/config` | beets config, with secrets redacted |
| `POST` | `/modifications` | Preview a `beet modify` |
| `GET` | `/modifications`, `/modifications/{id}` | Modification history, newest first |
| `POST` | `/modifications/{id}/confirm` | Apply a Preview |
| `POST` | `/imports` | Start an import from server paths |
| `GET` | `/imports/{id}` | Import progress and the current prompt |
| `POST` | `/imports/{id}/choice` | Answer an import prompt |
| `DELETE` | `/imports/{id}` | Abort an import |

Terms such as Item, Album, Modification and Preview are defined in [CONTEXT.md](CONTEXT.md). Design decisions are in [docs/adr](docs/adr).
