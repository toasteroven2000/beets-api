# beets-api

A small HTTP API for reading and changing a [beets](https://beets.io) music library, including the interactive import flow. It calls beets in-process, so it uses the same library, config and plugins as the `beet` command.

> [!WARNING]
> **This project is alpha.** Expect breaking changes to endpoints, responses and the history database at any time, with no notice and no migrations.

> [!CAUTION]
> **This project is AI coded and comes with no security guarantees.** Most of the code was written by an AI assistant and has not had a security review. The API has no authentication: anything that can reach it can read your library, change metadata, move files and import music. It accepts any `Host` header, so nothing but the network stops a website from reaching it. Only run it on `127.0.0.1` on a machine you trust, or on an internal-only address, and back up your library before pointing it at one you care about.

## Running

Dependencies, including beets itself, come from [devenv](https://devenv.sh):

```sh
devenv shell
serve                # http://127.0.0.1:8338, extra arguments go to uvicorn
devenv up            # or run it as the `web` process
python -m pytest     # tests
```

In the dev shell, `BEETSDIR` points at `db/foobar`, a throwaway library, and `BEETS_API_TRASH_DIR` at `db/trash`, so your real one in `~/.config/beets` is left alone. Outside devenv, the API uses whichever library `BEETSDIR` or beets' default config points to. The API keeps its own SQLite file, `beets-api.db`. It holds the state of Modifications, Removals, Purges and Imports, and the **audit**: one append-only entry for every Album or Item the API changes, imports, removes, or sets art for. Database triggers refuse any edit or deletion of audit entries. Changes made with `beet` directly are not audited.

Send `X-Beets-API-Client` (the program) and `X-Beets-API-Actor` (the person it acts for) to record who made a change. beets-api has no authentication, so both are labels, not proof: it relies on only trusted callers, such as beets-ui behind a proxy that signs people in, being able to reach it. The Actor is at most 200 characters with no control characters, or the request is refused with `400`. Each Choice of an Import records its own Actor, and the Audit entry for a change takes the Actor of the Choice that caused it: whoever picked the Candidate for an `album.import`, whoever chose "remove" for a duplicate's `album.remove`. A Modification, Removal or Purge takes whoever confirmed it.

Album art is only served, written or moved inside the music `directory` from beets' config. Nothing the API does deletes a file: removals, Import undo, a duplicate replaced by an Import, and replaced or removed art all move the files into the **Trash**, and each audit entry lists where they went (`trashed_to`). Only a Purge deletes from the Trash. The Trash is a directory you set with `BEETS_API_TRASH_DIR`; while it is unset a warning is logged at startup and anything that would delete a file is refused with `409`. Each operation gets a folder named by its id, keeping each file's path relative to the music directory; art changed on its own goes in an `art-album-…` folder. If the Trash is on another filesystem, a file is copied, checked, and only then removed from the library.

Nothing that can take long runs inside a request. Previews, Confirms, Purges and undoing an Import answer `202` at once and do the work in the background, so poll the operation's `GET` for its `status`. A Preview is `previewing` until it is built, then `awaiting_confirmation` (or `no_changes`, or `failed` with `result.error`); a Confirm goes `queued`, `applying`, then `applied`, `partial` or `rejected`. A malformed query or field list is still refused with `4xx` straight away. Page sizes (`limit`) are capped at 1000, and `GET /import-paths` pages a directory's entries with `limit` and `offset`. Operations still `previewing`, `queued` or `applying` when the server stops become `interrupted`.

Interactive API docs are served at `/docs`.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `BEETSDIR` | beets' default (`~/.config/beets`) | The beets config directory, and so the library, music `directory` and plugins the API uses. Read by beets itself. The dev shell sets it to `db/foobar`; the Docker image sets it to `/config`. |
| `BEETS_API_DB` | `beets-api.db` in the beets config directory | Path of the API's own SQLite file: Modification, Removal, Purge and Import state, and the audit. |
| `BEETS_API_TRASH_DIR` | unset: nothing that deletes a file is allowed | Directory that removed files are moved to instead of being deleted. A warning is logged if it is inside the music directory or an Import Path, where an Import could pick the files up again. |
| `BEETS_API_IMPORT_PATHS` | unset: any path | Directories `POST /imports` may read from, separated by `:` like `PATH`. A requested path must resolve, after following symlinks, to one of these directories or somewhere inside it; otherwise the request is refused with `403`. Set but empty, it refuses every Import. Unset, Imports may read any path the server can, and a warning is logged at startup. |

## Kubernetes

A Helm chart is in [charts/beets-api](charts/beets-api): `helm install beets-api charts/beets-api`. The container runs as user 65532 with a read-only root filesystem and no capabilities, with the library, the music and the Trash on volumes (`storage.enabled: false` uses throwaway `emptyDir`s instead). It is always one replica, and the Deployment strategy is `Recreate` unless you set `strategy`. There is no Ingress, because the API has no authentication: keep the Service internal, or put something that authenticates in front of it. The API's own settings are under `beetsApiConfig`: `trashDir` (the Trash's mount path, `/trash` by default; empty means no Trash, so nothing that deletes a file is allowed) and `importPaths`. `beetsConfig.dir` is beets' `BEETSDIR` (`/config` by default), where the config volume is mounted. beets' `config.yaml` comes from `beetsConfig.existing` (a ConfigMap or Secret you manage, for API keys), from `beetsConfig.raw` (inline, kept in a ConfigMap), or, with `raw` empty, from the `/config` volume itself, where it must set `directory: /music`. `extraEnvs` adds environment variables, after the chart's own so they can override them. The probes use `GET /ping`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| `GET` | `/list?scope=item\|album&query=…` | Items or Albums matching a beets query, paged (`limit` is at most 1000) |
| `GET` | `/items/{id}`, `/albums/{id}` | One Item or Album |
| `GET` | `/albums/{id}/art` | The Album's cover image |
| `PUT` | `/albums/{id}/art` | Upload a JPEG or PNG as `cover.jpg`/`cover.png` (never embedded) |
| `DELETE` | `/albums/{id}/art` | Remove the Album's art |
| `GET` | `/audit?target=album:5&action=…&actor=…` | Every change made to the Library, newest first |
| `GET` | `/audit/{id}` | One Audit entry; `404` if there is none |
| `GET` | `/config` | beets config, with secrets redacted |
| `POST` | `/modifications` | Start a `beet modify` Preview: `202` at once, then poll until it is no longer `previewing` |
| `GET` | `/modifications`, `/modifications/{id}` | Modification history, newest first |
| `POST` | `/modifications/{id}/confirm` | Apply a Preview: `202` at once, then poll. If the Library changed since the Preview, the Modification becomes `rejected` |
| `POST` | `/removals` | Start a Preview of removing the Items or Albums a query matches, and moving their files to the Trash. A query is required |
| `GET` | `/removals`, `/removals/{id}` | Removal history, newest first |
| `POST` | `/removals/{id}/confirm` | Remove what a Preview lists. Files inside the music directory go to the Trash; any outside it are left in place and listed in the audit entry |
| `GET` | `/trash` | The Trash: a folder per operation, with its file count, size, age, and the actions and Actors the audit knows for it |
| `POST` | `/purges` | Start a Preview of deleting Trash folders for good: `folders` (names from `/trash`) and/or `older_than`; with neither, every folder |
| `GET` | `/purges`, `/purges/{id}` | Purge history, newest first |
| `POST` | `/purges/{id}/confirm` | Delete what a Preview lists. Cannot be undone; becomes `rejected` if the Trash changed since the Preview |
| `POST` | `/imports` | Start an import from server paths |
| `GET` | `/imports`, `/imports/{id}` | Import history, newest first; one Import's progress and current prompt. `result.expected_tasks` is how many Import Tasks beets will ask about, counted when the Import starts (one per folder of music, or per file when importing singletons, leaving out what a resumed Import skips), and `result.tasks_done` how many it has finished, skipped ones included. The total is an estimate: choosing "Import as tracks" or "Group albums" for a folder adds Tasks, and it is `null` if counting failed |
| `GET` | `/imports/{id}/tasks?state=…` | The Import Tasks an active Import expects, in the order beets will ask, each `done`, `current` or `pending` (filter with repeatable `state`; paged with `limit` and `offset`). Empty once the Import ends, or if the Tasks could not be counted |
| `POST` | `/imports/{id}/choice` | Answer an import prompt |
| `GET` | `/imports/{id}/undo` | Preview undoing an Import: the Albums, Items and files it would remove, and `reasons` if it may not be undone. Changes nothing |
| `POST` | `/imports/{id}/undo` | Remove what an Import added and move the files it copied in to the Trash; `409` with the reasons when unsafe, otherwise `202` and it runs in the background: poll the Import, whose `undoing` is true until it finishes, then `result.undone` is set (or `result.undo_error`). Only a plain `copy` Import can be undone, and not once anything it made has been changed, or if it replaced existing records |
| `DELETE` | `/imports/{id}` | Abort an import |

Terms such as Item, Album, Modification and Preview are defined in [CONTEXT.md](CONTEXT.md). Design decisions are in [docs/adr](docs/adr).
