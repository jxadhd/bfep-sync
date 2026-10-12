# BFEP fundraiser sync

Keeps the raised and goal amounts of BFEP campaigns up to date from the
fundraising platforms. It started as the GoFundMe sync on the production
server (the first commit) and was refactored so more platforms can be added
as plug-ins. It runs on butterfly from `/opt/bfep-sync`; see `ROLLOUT.md`.

- `original/` holds the first `sync.py` and `gofundme.py` unchanged. The tests compare the new code against them, so don't edit them.
- `bfep_sync/` is the upgraded package.
- `tests/` has fixture-based tests. They make no network calls and use no database.
- `CHANGES.md` lists each change, with how to keep or drop it.
- `deploy/` has the systemd service and timer for a fresh install, the
  settings file template, an optional least-privilege database role
  (`grants.sql`), a cache hook (`after-sync.sh`) and a script that compares
  two versions' dry runs.
- `.github/workflows/test.yml` runs every test, including the PostgreSQL ones,
  on each push and pull request.
- `ROLLOUT.md` is the step-by-step move to production, with rollback.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

You need Python 3.10 or newer, and only the standard library. The tests also
check that the new GoFundMe parser returns exactly what the original parser
returns for the same pages.

The database tests in `tests/test_postgres.py` are skipped unless you point
them at a scratch PostgreSQL server (never production). They create and drop
their own `bfep_sync_test_…` databases, apply `deploy/grants.sql`, and run the
real SQL as the restricted `bfep_sync` role. They also run the original
`sync.py` and the new package side by side on identical databases and check
that the output and the resulting rows are the same. They need psycopg2.

```sh
BFEP_SYNC_TEST_DSN='host=/tmp port=5432 user=postgres' python3 -m unittest discover -s tests -v
```

## Run the sync

The usage matches the original. It is still a dry run unless you pass `--apply`.

```sh
pip install -r requirements.txt          # psycopg2-binary
export PGHOST=… PGDATABASE=… PGUSER=… PGPASSWORD=…
python3 sync.py --limit 5                 # dry run, same output format as before
python3 sync.py --apply --after "drush cr"
python3 -m bfep_sync --provider gofundme  # same thing, as a module
```

New options and settings:

| Option or variable | Default | Meaning |
|---|---|---|
| `--provider a,b` or `SYNC_PROVIDERS` | `gofundme` | Which providers run. An unknown name exits with code 4. |
| `--retries N` | `1` | Retries for network errors and HTTP 5xx on each campaign. `0` restores the original behaviour. |
| `JUSTGIVING_APP_ID` | unset | Turns on the JustGiving provider (see below). |

Exit codes: `0` means ok. `2` means a platform blocked us with 403 or 429; the
original also used 2 for this. `3` means another `--apply` run holds the lock.
`4` means a configuration error. `5` means the database could not be reached.

## The `bfep` command on the server

`bin/bfep` gathers the server's BFEP commands under one name. Each command is
a script in `commands/`:

| Command | What it does |
|---|---|
| `bfep deploy [branch]` | Deploys the website's custom modules and themes (the former `bfep-deploy`). |
| `bfep update` | Updates Drupal core and contrib by rebuilding the image, with backup and rollback (the former `bfep-update`). |
| `bfep sync [--apply] [--limit N]` | Runs the sync now with the settings in `/etc/bfep-sync.env`. A dry run unless `--apply`. |
| `bfep sync status` | The deployed version, the timer and the last run. |
| `bfep sync log [N]` | The last N lines of the sync's log. |
| `bfep sync test` | Runs the test suite on the live copy. |
| `bfep sync deploy [ref]` | Deploys a branch or tag of this repo to `/opt/bfep-sync`, tests first. `DRY_RUN=1` shows what would change. |
| `bfep sync rollback` | Swaps back to the copy from before the last deploy. Run it again to undo. |
| `bfep backup` | Dumps the databases and uploads them to Cloudflare R2, never past the free tier (see below). |
| `bfep help [command]` | Lists the commands, or shows one command's options. |

`bfep sync deploy` works like `bfep deploy`. It fetches this repo into
`/opt/bfep-sync-src`, runs the tests, copies the files over `/opt/bfep-sync`
(keeping the previous copy in `/opt/bfep-sync.prev` and leaving any `*-wip`
folders alone) and records the version in `/opt/bfep-sync/.deployed`. It
refuses to run while a sync run holds `/run/bfep-sync.lock`, so code is never
swapped mid-run. The systemd timer keeps running `/opt/bfep-sync/sync.py`, so
nothing else changes.

A command this repo doesn't have yet still works: `bfep foo` runs an existing
`/usr/local/bin/bfep-foo`. To add a command, put an executable script in
`commands/` whose second line is `# name: one-line description`.

### Installing it (once)

```sh
git clone https://github.com/jxadhd/bfep-sync /opt/bfep-sync-src
/opt/bfep-sync-src/install.sh
```

`install.sh` runs `bfep sync deploy` from the checkout and links
`/usr/local/bin/bfep` to `/opt/bfep-sync/bin/bfep`, so every later deploy also
updates the command. `bfep-deploy` and `bfep-update` become one-line wrappers
for `bfep deploy` and `bfep update`, and the originals are copied to
`/usr/local/lib/bfep/replaced/`. It is safe to run again.

## Database backups to Cloudflare R2

`bfep backup` dumps each database in `BACKUP_DATABASES` with `pg_dump` from a
throwaway `postgres:18-alpine` container, checks each dump reads back, packs
them into one `bfep-db-<UTC time>.tar` (gpg-encrypted if
`BACKUP_PASSPHRASE_FILE` is set) and uploads it to R2 as a single object. The
`bfep-backup.timer` runs it daily at 04:15 NZ time. The R2 side is
`bfep_backup/`, standard library only.

It is built so it cannot cost money:

- **Storage.** Before uploading it lists the whole bucket. Everything in the
  bucket counts toward `BACKUP_MAX_GB` (default 8, and anything above 9 is
  rejected, against the free 10 GB-month). Expired backups are deleted first,
  then the oldest ones if more room is needed, all before the upload, so the
  bucket never goes over the limit at any moment. The newest `BACKUP_KEEP_MIN`
  are never deleted to make room; the upload is refused instead (exit 6) and
  the local copy is kept. Only our own `bfep-db-*` objects under
  `BACKUP_PREFIX` are ever deleted.
- **Requests.** Every request is counted in
  `/var/lib/bfep-backup/r2-requests.json` before it is sent, and refused once
  the month's count would pass `BACKUP_MAX_CLASS_A` or `BACKUP_MAX_CLASS_B`
  (default 1000 each; the free tier is 1 million and 10 million). A damaged
  count file stops all requests until someone looks at it. A daily run makes
  about 2 Class A requests (one listing, one upload); deletes are free.
- **Storage class.** Uploads ask for `STANDARD`. Infrequent Access is not in
  the free tier and bills retrievals.
- **No multipart uploads**, so no half-finished uploads hold storage. One
  backup over `BACKUP_MAX_OBJECT_GB` (default 2) is refused.

| Command | What it does |
|---|---|
| `bfep backup` | Dump and upload. `DRY_RUN=1` dumps and shows what it would upload or delete, without doing it. |
| `bfep backup check` | Checks the settings, connects to each database read-only and lists the bucket. |
| `bfep backup list` | The backups in the bucket, its total and this month's request counts. |
| `bfep backup usage` | This month's request counts, without contacting R2. |
| `bfep backup fetch <name> [dir]` | Downloads one backup (one Class B request). |
| `bfep backup status` | The timer and the last run. |

Settings: `/etc/bfep-backup.env` (template `deploy/bfep-backup.env.example`)
and `/etc/bfep-backup.pg_service.conf` (template
`deploy/bfep-backup.pg_service.conf.example`), both root-only; the command
refuses to run if either is readable by others. `deploy/backup-role.sql`
creates an optional read-only `bfep_backup` database role.

To restore, fetch a backup, unpack it (`gpg -d` first if encrypted), check
`sha256sum -c SHA256SUMS`, then `pg_restore --no-owner -d <database> <name>.dump`
into a scratch database first.

## How it is built

```
bfep_sync/
  providers/base.py       Provider interface, Figures, ParseError, Blocked, NotConfigured
  providers/gofundme.py   GoFundMe: original normalize() and parse_gofundme(), unchanged rules
  providers/gogetfunding.py GoGetFunding page text parser (off until listed)
  providers/justgiving.py JustGiving via its REST API (off until configured)
  providers/paypal.py     PayPal fundraiser (donate?campaign_id=) page data parser (off until listed)
  providers/spotfund.py   Spotfund amount-raised section parser (off until listed)
  providers/__init__.py   registry and SYNC_PROVIDERS handling
  runner.py               check() decision rules, retries, the run loop
  store.py                all SQL (the only psycopg2 import) and the advisory lock
  __main__.py             command line
sync.py                   drop-in entry point with the same name as the original
```

A provider answers three questions. Which stored URLs are mine
(`url_pattern`, a PostgreSQL regex used in the SELECT)? What is the canonical
URL (`normalize`)? What are the figures (`fetch_figures`)? Every safeguard
lives in `runner.check()`, so a new provider gets them automatically.

### Safeguards kept exactly as before

All of these are covered by tests in `CheckRulesTest`:

- A raised figure that falls by more than 20% is held as `suspicious_drop`.
- When the page states a currency that differs from the stored one, or none is
  stored, the page's currency, goal and amount are adopted, and the run prints
  `CURRENCY old -> new` for the change. The 20% drop check is skipped for that
  row, since the amounts are in different currencies. A page that shows only a
  bare "$" (GoGetFunding, or Spotfund without a story currency) keeps the stored currency when it is a dollar
  currency, and gives `currency_unknown` otherwise. Set `SYNC_HOLD_CURRENCY=1`
  to go back to the original rule (hold as `currency_mismatch` unless the goal
  matches, and `currency_unknown` when none is stored).
- 404 or 410 gives `not_found_pending` until 3 consecutive failures, then `not_found`.
  A parse error also counts as a failure.
- 403 or 429 stops calling that platform for the rest of the run, with exit code 2.
- The same UPDATE statements, the same columns and the same dry-run output lines.
- About 6 seconds plus 0 to 2 seconds of jitter between GoFundMe requests, and no
  sleep after an invalid URL.

## Adding a provider

1. Create `bfep_sync/providers/<name>.py` with a `Provider` subclass. Set `name`,
   `url_pattern` and `delay`, then implement `normalize` and `fetch_figures`.
   Raise `ParseError` when a page has no usable figures. If it needs a key,
   read it from the environment and return `False` from `configured()` when it
   is missing.
2. Register it in `PROVIDERS` in `providers/__init__.py`.
3. Save a real page or response as a fixture in `tests/fixtures/` and add parser tests.
4. Dry-run it on a few rows: `python3 -m bfep_sync --provider <name> --limit 3`.
5. Add it to `SYNC_PROVIDERS` in the server's env file.

### Candidate next providers

| Platform | Approach | Status |
|---|---|---|
| GoGetFunding | Page scrape of the visible sidebar text | Implemented, off by default, **fixtures are synthetic: dry-run before enabling** |
| Spotfund | Page scrape of the amount-raised section, currency from the story data | Implemented, off by default, fixture saved from a live page |
| PayPal fundraisers | Page data fields next to the campaign's productId; paypal.me and donate buttons have no totals and are left out | Implemented, off by default, **fixture is synthetic: dry-run before enabling** |
| JustGiving | Official REST API, needs a free app ID | Implemented, off by default, **unverified against the live API** |
| Chuffed | Page scrape (Next.js data, like GoFundMe) | Not started: needs a real page saved as a fixture |
| LaunchGood | Page scrape | Not started: needs a fixture |
| GiveSendGo | Page scrape or JSON endpoint | Not started: needs a fixture |
| Whydonate | Page scrape | Not started: needs a fixture |

The scraped platforms can't be written safely without a real saved page to
test against, and this session had no access to those sites. To start one,
save a campaign page with "Save page as…", put it in `tests/fixtures/`, and the
parser can be written from it.

## Deployment, and moving to a repo

- Follow `ROLLOUT.md`.
- `deploy/bfep-sync.service` and `deploy/bfep-sync.timer` run the sync twice a
  day with a random delay. Credentials go in a root-only `EnvironmentFile`
  (template: `deploy/bfep-sync.env.example`).
- `deploy/after-sync.sh` runs after a run that changed figures. It invalidates
  the `bfep:campaigns`, `bfep:countries` and `bfep:home` cache tags with drush,
  instead of rebuilding every cache with `drush cr`.
- The advisory lock (id 42633702) stops two `--apply` runs overlapping. Dry
  runs don't take it. The website's add-campaign lock uses a different id
  (42633701).
- To update the server, run `bfep sync deploy`. For a change that could
  alter what the sync writes, first run `DRY_RUN=1 bfep sync deploy`, then
  compare the old and new dry runs with `deploy/compare-dry-run.sh`
  (from `/opt/bfep-sync-src`, against `/opt/bfep-sync`).

## What was tested here

- 49 tests pass on Python 3.13 with PostgreSQL 16: 27 fixture tests, 9
  database tests (over a Unix socket and over TCP with password login) and 13
  tests of the `bfep` command and `install.sh` against fake server folders.
- The database tests ran as a `bfep_sync` role restricted by `grants.sql`,
  with tables owned by `data`, as on production. The role could not read
  other columns, insert, or update the URL.
- Original and new, run side by side on identical databases through a dry
  run and three `--apply` runs, printed identical lines and left identical
  rows.
- Nothing touched production, real data or a real platform. GoFundMe could
  not be reached from the test machine, so the parser has only been checked
  against saved pages; step 5 of `ROLLOUT.md` checks it against live pages.
