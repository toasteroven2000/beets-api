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

In the dev shell, `BEETSDIR` points at `db/foobar`, a throwaway library, so your real one in `~/.config/beets` is left alone. Outside devenv, the API uses whichever library `BEETSDIR` or beets' default config points to. The API keeps its own SQLite file, `beets-api.db`. It holds the state of Modifications and Imports, and the **audit**: one append-only entry for every Album or Item the API changes, imports, removes, or sets art for. Database triggers refuse any edit or deletion of audit entries. Changes made with `beet` directly are not audited.

Send `X-Beets-API-Client` (the program) and `X-Beets-API-Actor` (the person it acts for) to record who made a change. beets-api has no authentication, so both are labels, not proof: it relies on only trusted callers, such as beets-ui behind a proxy that signs people in, being able to reach it. The Actor is at most 200 characters with no control characters, or the request is refused with `400`. Each Choice of an Import records its own Actor, and the Audit entry for a change takes the Actor of the Choice that caused it: whoever picked the Candidate for an `album.import`, whoever chose "remove" for a duplicate's `album.remove`. A Modification takes whoever confirmed it.

Album art is only served, written or archived inside the music `directory` from beets' config, and changes are never destructive: a replaced or removed image is copied into the `art_archive` table in `beets-api.db` before its file is deleted, and the audit entry gives the archive row's id. Like the audit, archived images can't be edited or deleted.

Interactive API docs are served at `/docs`.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `BEETSDIR` | beets' default (`~/.config/beets`) | The beets config directory, and so the library, music `directory` and plugins the API uses. Read by beets itself. The dev shell sets it to `db/foobar`; the Docker image sets it to `/config`. |
| `BEETS_API_DB` | `beets-api.db` in the beets config directory | Path of the API's own SQLite file: Modification and Import state, the audit and the art archive. |
| `BEETS_API_IMPORT_PATHS` | unset: any path | Directories `POST /imports` may read from, separated by `:` like `PATH`. A requested path must resolve, after following symlinks, to one of these directories or somewhere inside it; otherwise the request is refused with `403`. Set but empty, it refuses every Import. Unset, Imports may read any path the server can, and a warning is logged at startup. |

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/list?scope=item\|album&query=…` | Items or Albums matching a beets query, paged |
| `GET` | `/items/{id}`, `/albums/{id}` | One Item or Album |
| `GET` | `/albums/{id}/art` | The Album's cover image |
| `PUT` | `/albums/{id}/art` | Upload a JPEG or PNG as `cover.jpg`/`cover.png` (never embedded) |
| `DELETE` | `/albums/{id}/art` | Remove the Album's art |
| `GET` | `/audit?target=album:5&action=…&actor=…` | Every change made to the Library, newest first |
| `GET` | `/config` | beets config, with secrets redacted |
| `POST` | `/modifications` | Preview a `beet modify` |
| `GET` | `/modifications`, `/modifications/{id}` | Modification history, newest first |
| `POST` | `/modifications/{id}/confirm` | Apply a Preview |
| `POST` | `/imports` | Start an import from server paths |
| `GET` | `/imports`, `/imports/{id}` | Import history, newest first; one Import's progress and current prompt |
| `POST` | `/imports/{id}/choice` | Answer an import prompt |
| `GET` | `/imports/{id}/undo` | Preview undoing an Import: the Albums, Items and files it would remove, and `reasons` if it may not be undone. Changes nothing |
| `POST` | `/imports/{id}/undo` | Remove what an Import added and the files it copied in; `409` with the reasons when unsafe. Only a plain `copy` Import can be undone, and not once anything it made has been changed, or if it replaced existing records |
| `DELETE` | `/imports/{id}` | Abort an import |

Terms such as Item, Album, Modification and Preview are defined in [CONTEXT.md](CONTEXT.md). Design decisions are in [docs/adr](docs/adr).
