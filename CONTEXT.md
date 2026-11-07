# Beets API

An HTTP API for reading and changing a beets library, including the import flow. It uses beets' own terminology wherever beets has a word for the concept.

## Language

### Library

**Library**:
The beets database of music, together with the directory holding its files.
_Avoid_: Collection, catalog

**Item**:
A single music file known to the Library.
_Avoid_: Track, song

**Album**:
A group of Items stored as one record in the Library, whose album-level fields can be copied down to its Items.
_Avoid_: Release, record

### Changing metadata

**Modify**:
A change to the metadata of the Items or Albums matched by a query, equivalent to `beet modify`.
_Avoid_: Edit, batch edit

**Modification**:
One requested Modify, from its Preview through to its outcome.
_Avoid_: Edit, job

**Preview**:
The exact changes a Modification would make, shown before anything changes.
_Avoid_: Diff, pretend, dry run

**Confirm**:
Approving a whole Preview so its changes get applied.
_Avoid_: Approve, commit

### Importing

**Import**:
Bringing music files from server paths into the Library. This is a core beets command, not a Plugin.
_Avoid_: Ingest, upload

**Import Task**:
One Album's or one Item's worth of files within an Import, which needs its own Choice.
_Avoid_: Job

**Candidate**:
A possible metadata match for an Import Task, found by a metadata source.
_Avoid_: Result, suggestion

**Choice**:
What to do with an Import Task, such as applying a Candidate, importing as-is, or skipping.
_Avoid_: Decision, answer

### Beets extensions

**Plugin**:
An optional beets extension enabled in config, which can add commands, metadata sources or import steps.
_Avoid_: Addon, extension
