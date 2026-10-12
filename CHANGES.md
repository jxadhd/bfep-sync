# Changes from the original, one by one

Each item says what changed, why, and how to keep or drop it. The first
commit is the original; `git diff` against it shows every line.

## 1. Pluggable providers (structural)

**What:** The GoFundMe-specific code (`HOSTS`, `normalize`, `parse_gofundme`)
moved into `providers/gofundme.py` behind a `Provider` interface. The runner
works with any registered provider. The SELECT's URL regex is built from the
enabled providers. `Figures` and `ParseError` moved to `providers/base.py`.

**Behaviour:** Identical for GoFundMe. The parser body is unchanged, and a test
compares it with `original/gofundme.py` on the same fixtures.

**Drop it:** This is the foundation. To drop all of it, keep using `original/`.

## 2. Database code isolated in `store.py`

**What:** The SELECT and the two UPDATEs moved, unchanged, into
`PostgresStore`. The runner talks to a store object, so tests use a fake store
and never need psycopg2 or a database.

**Behaviour:** Identical SQL. The one difference is that the URL regex is now a
bound parameter built from the enabled providers.

## 3. Block handling per provider

**What:** A 403 or 429 still stops all further requests to that platform for
the run, and the exit code is still 2. With several providers enabled, the
others carry on. The message is now `BLOCKED by gofundme (HTTP 429), skipping
its remaining campaigns`.

**Behaviour with GoFundMe only:** The same as the original abort. The rest of
the rows are skipped and the summary is still printed.

**Drop it:** In `runner.run()`, replace the `continue` after a block with `break`.

## 4. Retry of transient errors (new; `--retries`, default 1)

**What:** Network errors and HTTP 500, 502, 503 and 504 are retried once after
5 seconds before being recorded as `error` or `http_5xx`. 403, 404, 410, 429
and parse errors are never retried.

**Why:** One timeout used to cost a campaign a whole cycle.

**Drop it:** Run with `--retries 0`, or change the default in `__main__.py`.

## 5. Overlap lock (new)

**What:** `pg_try_advisory_lock(42633702)` at startup. If another run holds
the lock, the run exits with code 3 without doing anything.

**Why:** A slow run (about 6 seconds per campaign) can still be going when
cron starts the next one, and both would then write the same rows.

**Drop it:** Remove the `store.lock()` call in `__main__.py`.

## 6. Configuration and exit codes (new)

**What:** `--provider` and `SYNC_PROVIDERS` choose the providers. An unknown
name exits with code 4. A provider missing its settings is skipped with a
message. A row whose provider is not configured is recorded as
`not_configured`.

## 7. JustGiving provider (new, off by default)

**What:** Reads figures from JustGiving's official API. It needs
`JUSTGIVING_APP_ID` and is only active when listed in `SYNC_PROVIDERS`. It has
not been checked against the live API, so dry-run it first.

**Drop it:** Delete `providers/justgiving.py` and its entry in
`providers/__init__.py`.

## 8. Tests, deploy examples and CI template (new)

`tests/` (27 fixture tests), `deploy/bfep-sync.service`, `deploy/bfep-sync.timer` and
a CI workflow (now `.github/workflows/test.yml`). None of these affect runtime.

## 9. Dry runs take no lock (2026-10-10)

**What:** Only `--apply` runs take the advisory lock. A dry run started while
the timer is running no longer makes the timer's run exit with code 3.

**Drop it:** In `__main__.py`, call `store.lock()` unconditionally again.

## 10. Clean exit when the database is unreachable (2026-10-10)

**What:** A failed connection prints `database connection failed: …` and
exits with code 5, instead of a Python traceback and code 1.

## 11. Cache hook by tag (2026-10-10)

**What:** `deploy/after-sync.sh` invalidates the `bfep:campaigns`,
`bfep:countries` and `bfep:home` cache tags with drush. The site's campaign,
home and listing pages carry these tags, so new figures show without a full
`drush cr`. `BFEP_DRUSH` says how drush is called (plain or via Docker).

**Drop it:** Use `--after "drush cr"` in the service, as before.

## 12. Production deployment kit (2026-10-10)

`ROLLOUT.md`, `deploy/grants.sql` (optional least-privilege role),
`deploy/bfep-sync.env.example`, `deploy/compare-dry-run.sh`, and a hardened
`deploy/bfep-sync.service` (timeout, NoNewPrivileges, ProtectSystem). None of
these change what the sync does.

## 13. Database tests (2026-10-10)

`tests/test_postgres.py`: the real SQL, grants, lock, `--after` hook and exit
codes against a scratch PostgreSQL, plus a side-by-side parity run against
`original/sync.py`. Skipped unless `BFEP_SYNC_TEST_DSN` is set. The CI
template runs them in a PostgreSQL service container. To let tests patch
them, `run()` and `check()` now look up `time.sleep`, `random.random` and the
HTTP fetcher when called rather than binding them as default arguments; the
behaviour is unchanged.

## 14. The `bfep` command (2026-10-10)

**What:** `bin/bfep` with `commands/sync` (run, status, log, test, deploy,
rollback) and `install.sh`. `bfep sync deploy` deploys this repo the way
`bfep-deploy` deploys the website. `tests/test_cli.py` covers it against fake
server folders.

**Drop it:** Delete `bin/`, `commands/sync`, `install.sh` and
`tests/test_cli.py`, remove `/usr/local/bin/bfep`, and copy the originals back
from `/usr/local/lib/bfep/replaced/`.

## 15. `bfep deploy` and `bfep update` (2026-10-10)

**What:** The server's `bfep-deploy` and `bfep-update` scripts, now in
`commands/` so they are under version control. Only their header comments
changed.

**Drop it:** Delete `commands/deploy` and `commands/update`; `bfep deploy`
then falls back to the original scripts once they are copied back.

## 16. GoGetFunding provider (2026-10-10, off by default)

**What:** `providers/gogetfunding.py` reads raised, goal, currency and donor
count from a GoGetFunding page's visible text. The parser is Josh's
`gogetfunding-wip` parser, adapted to the shared `Figures` and `ParseError`.
It only runs when `gogetfunding` is in `SYNC_PROVIDERS`. When a page shows
only a bare "$", `runner.check()` keeps the row's stored currency if it is a
dollar currency (USD, AUD, NZD, CAD, HKD, SGD) and records `currency_unknown`
otherwise, so a "$" page never relabels a row. The test fixture is built from
the page's text shapes, not saved from the site, so dry-run it before enabling.
Since 2026-10-12 it also reads campaigns without a goal and closed ones,
which show "US$589.00 Donated So Far" (and, when there are offline gifts,
"Raised offline: $505.00 Total: $3,411.00", where the total is used).

**Drop it:** Delete `providers/gogetfunding.py`, its entry in
`providers/__init__.py`, the bare-"$" branch in `runner.check()` and its tests.
