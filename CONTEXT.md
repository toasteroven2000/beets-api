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

**Scope**:
Whether a query (in a Modify or a listing) matches Items or Albums, like `beet modify` with or without `-a`. Album-scoped changes to shared fields are copied down to the Album's Items.
_Avoid_: Album flag, mode

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

**Import Path**:
A directory beets-api allows Imports to read from. Whatever is picked for an Import lies inside one.
_Avoid_: Inbox, source folder, drop folder

**Import Task**:
One Album's or one Item's worth of files within an Import, which needs its own Choice.
_Avoid_: Job

**Candidate**:
A possible metadata match for an Import Task, found by a metadata source.
_Avoid_: Result, suggestion

**Choice**:
What to do with an Import Task, such as applying a Candidate, importing as-is, or skipping.
_Avoid_: Decision, answer

### Audit

**Audit**:
The append-only record of every change the API made to the Library. Previews, rejected Modifications and skipped Import Tasks change nothing, so they are not in it.
_Avoid_: History, log, revisions

**Audit entry**:
One change to one Album or Item (including its art): when, by which Client and Actor, what changed, and the Modification or Import that caused it.
_Avoid_: Event, revision

**Client**:
The program making a request, named by the caller in the `X-Beets-API-Client` header (`X-Client` before it was prefixed). Without authentication it is a label, not proof.
_Avoid_: User, caller

**Actor**:
The person a Client acted for, named in the `X-Beets-API-Actor` header. beets-api does not sign anyone in, so the Client asserts it, such as beets-ui reading it from the proxy that did. It is recorded on Audit entries and on each Choice of an Import, so every Library change says who made it. Empty when the Client names nobody.
_Avoid_: User, account, Client

### Beets extensions

**Plugin**:
An optional beets extension enabled in config, which can add commands, metadata sources or import steps.
_Avoid_: Addon, extension
