# Call beets' Python library in-process instead of running the CLI

The API calls beets' Python library directly rather than running `beet` as a subprocess, even though its purpose is to put HTTP in front of what the CLI does. The CLI's import prompts are text written for a terminal, so driving them from a subprocess would mean parsing human-readable output and typing answers into stdin. The importer instead has `ImportSession.choose_match`, a method built to be overridden, which the CLI's own terminal session also uses. `modify` is built from library functions the API can call too, so behaviour stays close to the CLI, including config, plugins and query syntax.

## Consequences

The API depends on beets internals that may change between releases, so the beets version is pinned through Nix to match the `beet` CLI in the same environment. Beets' config is global to the process, which limits the server to one active Import at a time.
