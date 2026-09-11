# Simplify Live Job Board Design

Date: 2026-09-11

## Purpose

Add a private local job board to ApplyPilot that keeps Summer 2027 listings synchronized from `SimplifyJobs/Summer2027-Internships`. The board must make source freshness visible, preserve application history, and never start applications merely because new listings were synchronized.

## Scope

The first version will:

- Fetch the repository's structured `.github/scripts/listings.json` feed.
- Import active, visible listings whose terms include `Summer 2027`.
- Import all Simplify categories so the user can filter Software, AI/ML/Data, Product, Quant, and Hardware in the board.
- Exclude Google and Coinbase at both synchronization and application-selection boundaries.
- Synchronize at startup when the local snapshot is older than two hours, then every two hours while the board is running.
- Provide a manual `Refresh now` action.
- Serve a private board on `127.0.0.1` and open it in the default browser.
- Show current openings, newly added roles, closures, source freshness, fit scores, and application state.

The first version will not:

- Automatically apply to newly synchronized roles.
- Broaden the user's authorized application set without a separate filter review.
- Solve or bypass CAPTCHAs.
- Host the dashboard publicly or expose applicant data outside the machine.
- Clone the full Simplify repository.

## Source Contract

The source URL is:

`https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json`

Each accepted record must have a non-empty `id`, `company_name`, `title`, `url`, and `date_posted`. `terms` must contain `Summer 2027`, while `active` and `is_visible` must both be true for the listing to appear as open.

The synchronizer will use `httpx` with a bounded timeout, a descriptive user agent, and conditional requests when the server provides `ETag` or `Last-Modified`. It will validate that the response is a JSON array and that the snapshot contains a plausible number of valid records before changing closure state. Network failures, rate limits, malformed JSON, or an implausibly empty snapshot leave the existing database untouched.

## Data Model

ApplyPilot's `jobs` table remains the pipeline's primary record. The forward-only migration registry in `database.py` will add these nullable columns:

- `source_name`: stable source label, `simplify-summer-2027` for this feed.
- `source_id`: Simplify listing UUID.
- `source_category`: Simplify category.
- `source_active`: current source activity flag.
- `source_posted_at`: UTC timestamp derived from `date_posted`.
- `source_updated_at`: UTC timestamp derived from `date_updated`.
- `source_synced_at`: UTC timestamp of the last successful observation.
- `source_terms`: JSON array retained for traceability.
- `source_sponsorship`: source sponsorship classification.
- `source_degrees`: JSON array of degree requirements.

A unique partial index on `(source_name, source_id)` will prevent duplicate Simplify records without changing the existing URL primary key.

A separate `source_sync_state` table will store one row per source with:

- source name
- last attempted time
- last successful time
- last HTTP status
- last error
- ETag and Last-Modified values
- accepted record count
- inserted, updated, reopened, and closed counts from the latest successful sync

## Synchronization Behavior

The new `applypilot.discovery.simplify` module will separate fetching, normalization, and persistence.

1. Fetch the complete JSON snapshot.
2. Validate and normalize records without mutating the database.
3. Remove Google and Coinbase case-insensitively.
4. Begin one SQLite transaction.
5. Match existing rows by `(source_name, source_id)`, falling back to URL only for records imported before source IDs existed.
6. Insert new records or update source-owned discovery fields on existing records.
7. Preserve all enrichment, scoring, tailoring, cover-letter, and application columns.
8. Mark previously synchronized Simplify records absent from a valid complete snapshot as `source_active = 0`; never delete them.
9. Commit job changes and successful sync metadata together.

If a listing reappears, the synchronizer marks it active again. A changed application URL is updated for the same source ID when it does not collide with another job. A collision is recorded as a sync error for that record without overwriting either job's application history.

The public sync result will report counts for received, accepted, excluded, inserted, updated, reopened, and closed records.

## CLI

Two commands will be added:

### `applypilot sync-simplify`

Runs one foreground synchronization and prints source freshness plus mutation counts. It exits nonzero on fetch or validation failure and does not leave a partial import.

### `applypilot board`

Starts the private board at `http://127.0.0.1:8765`, opens it in the default browser, and runs the two-hour synchronization loop. On startup it syncs only when the last successful snapshot is missing or at least two hours old. The command accepts `--port` for local conflicts and `--no-open` for terminal-only startup. The interface does not offer a non-loopback bind option.

Stopping the command stops the scheduler and HTTP server cleanly.

## Local HTTP Interface

The implementation will use Python's standard-library threaded HTTP server to avoid adding a web-framework dependency.

Endpoints:

- `GET /`: dashboard shell.
- `GET /api/jobs`: current job records required by the board.
- `GET /api/stats`: summary counts and source sync state.
- `POST /api/sync`: request an immediate sync; concurrent requests coalesce into the active sync.

The server binds only to `127.0.0.1`. API responses will not include the applicant profile, credentials, resume contents, generated cover-letter contents, or agent logs. The server will reject unsupported methods and cap response sizes through bounded database queries and explicit fields.

## Dashboard

The current static `view.py` dashboard will be split so query/serialization logic can be reused by the live server without duplicating SQL.

The board will show:

- open roles
- roles added today
- roles added in the last 7 and 30 days
- roles closed since the previous successful sync
- applied, pending, failed, and in-progress application counts
- last successful source sync, its age, and latest mutation counts

Job rows will include company, title, category, location, posting age, source status, fit score, application status, degree requirement, sponsorship classification, and a direct employer application link.

Client-side controls will filter by text, category, open/closed state, maximum age, degree requirement, location text, fit score, and application status. Google and Coinbase will never appear because they are rejected before persistence and also filtered from API queries as defense in depth.

The page polls local JSON endpoints every 30 seconds for display changes. This does not fetch GitHub every 30 seconds. Automatic external synchronization remains every two hours. `Refresh now` disables itself while a sync is active and displays either the completed counts or a readable error while retaining the previous data.

## Failure Handling

- A failed automatic sync records its error and keeps serving the last successful snapshot.
- Closure marking occurs only after a valid complete snapshot is available.
- SQLite writes use one transaction and the existing busy timeout/WAL configuration.
- The scheduler prevents overlapping syncs.
- HTTP handlers return structured errors without stack traces or secrets.
- A port conflict prints a clear message and suggests `--port`.
- Application status is never inferred from a source closure.

## Testing

Tests will use local JSON fixtures and temporary SQLite databases. Network access will be replaced at the HTTP-client boundary only.

Coverage will include:

- accepted Summer 2027 record normalization
- rejection of inactive, invisible, wrong-term, malformed, Google, and Coinbase records
- idempotent repeat synchronization
- preservation of scoring, tailoring, and application fields during source updates
- reopening and safe closure marking
- no closure changes after invalid or incomplete snapshots
- URL-change handling and collision protection
- sync-state timestamps and counters
- two-hour staleness decision with a controlled clock
- API field allowlists and exclusion defense
- manual refresh coalescing
- CLI success and failure exit behavior
- board rendering and filtering smoke coverage

The existing Codex application-runner tests must continue to pass.

## Files and Boundaries

Expected implementation areas:

- `src/applypilot/discovery/simplify.py`: fetch, normalize, and transactional sync.
- `src/applypilot/database.py`: forward migration fields, index, and sync-state table.
- `src/applypilot/board.py`: loopback HTTP server, scheduler, and API handlers.
- `src/applypilot/view.py`: reusable queries and dashboard shell/rendering.
- `src/applypilot/cli.py`: `sync-simplify` and `board` commands.
- `tests/fixtures/simplify_listings.json`: deterministic representative source records.
- `tests/test_simplify_sync.py`: source and persistence behavior.
- `tests/test_board.py`: API, scheduler, and rendering behavior.

The synchronizer owns only source/discovery metadata. The application launcher remains responsible for application selection and submission. The board reads application state but does not mutate it except through a future separately approved feature.

## Acceptance Criteria

The feature is complete when:

1. `applypilot sync-simplify` safely imports a fixture and a live snapshot using the defined filters.
2. Repeating the sync creates no duplicates and preserves existing application history.
3. A valid later snapshot closes missing source records without deleting them.
4. `applypilot board` serves only on loopback, opens successfully, and shows source freshness and live database changes.
5. Automatic GitHub fetches occur no more than once every two hours unless the user clicks `Refresh now`.
6. Google and Coinbase are absent from synchronization results, board APIs, and application acquisition.
7. Fetch or validation failure leaves the last successful job snapshot usable.
8. The full automated test suite passes.
