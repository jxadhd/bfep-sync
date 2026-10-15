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

## 17. Stated currencies are adopted (2026-10-12)

**What:** When a page states a currency that differs from the stored one, or
none is stored, the sync now takes the page's currency, goal and amount
instead of holding the row as `currency_mismatch` or `currency_unknown`. The
run prints `CURRENCY old -> new`, and the 20% drop check is skipped for that
row. Josh chose this for every platform on 2026-10-12. Bare "$" pages still
never relabel a row.

**Drop it:** Set `SYNC_HOLD_CURRENCY=1` in `/etc/bfep-sync.env` for the
original rule, or revert this commit.

## 18. Spotfund provider (2026-10-12, off by default)

**What:** Reads the raised amount, goal and donor count from a Spotfund
page's "amount-raised-section" block ("$31,282 of $65,000 goal 479 Donors"),
which the page shows twice; both copies must agree. The amounts carry a bare
"$", so the currency comes from the story's own `currency_code` in the page's
embedded data. Stored links are `spot.fund/<code>` short links (followed to
the story) or `www.spotfund.com/story/<id>` pages; query strings are dropped.
It only runs when `spotfund` is in `SYNC_PROVIDERS`. The fixture is trimmed
from fundraiser 682's live page, and the saved pages for 682, 687, 717 and
840 all parse to the figures shown on them.

**Drop it:** Delete `providers/spotfund.py`, its entry in
`providers/__init__.py`, `tests/fixtures/spotfund_ok.html` and its tests.

## 19. PayPal fundraiser provider (2026-10-12, off by default)

**What:** Reads PayPal fundraisers (`paypal.com/donate?campaign_id=...`)
from the data embedded in the page: `campaignTotalAmountRaised`,
`campaignTargetAmount` (empty or 0 means no goal), `campaignTotalNumberOfTxn`
as the donation count and `donationCode` as the currency. The page's
`productId` must equal the link's `campaign_id`, and each field must have one
value. Closed (INACTIVE) campaigns still report their final figures.
`paypal.me` profiles and `hosted_button_id` donate buttons show no totals, so
the provider's URL pattern leaves them out. Links are reduced to
`https://www.paypal.com/donate/?campaign_id=<id>`, dropping tracking
parameters. Field names come from fundraiser 1972's page as fetched on the
server; the fixture is synthetic, so dry-run before enabling.

**Drop it:** Delete `providers/paypal.py`, its entry in
`providers/__init__.py`, `tests/fixtures/paypal_ok.html` and its tests.

## 20. Chuffed provider (2026-10-15, off by default)

**What:** Reads Chuffed campaigns from Chuffed's private GraphQL API
(`https://chuffed.org/api/graphql`), which Chuffed opened for our server.
Each request sends the token they issued in an `x-chuffed-token` header,
read from `CHUFFED_TOKEN` in `/etc/bfep-sync.env`; without it the provider is
not configured and its rows are skipped. A campaign is looked up by the
number at the start of its link (`/project/120680-...`); links without one
are `invalid_url`. Amounts come back in cents (except zero-decimal
currencies) and the raised and goal currencies must match. A campaign the
API doesn't know counts as not found, so the usual 3-strikes rule applies.
Chuffed allows 10 requests a minute: the provider waits at least 6.5 s
between its own requests, retries included. `http_fetch` can now POST.

**Drop it:** Delete `providers/chuffed.py`, its entry in
`providers/__init__.py` and its tests.

## 21. Chuffed in batches, and links without a number (2026-10-15)

**What:** Before the run, the Chuffed provider asks for 20 campaigns per
request, each under its own alias (`c0: campaign(id: "...")`), so about 205
campaigns take 11 requests (just over a minute) instead of one request every
7 s (about 25 minutes). A campaign a batch didn't answer is fetched on its
own, as before. Every request still waits 6.5 s after the previous one, and
the runner no longer adds its own wait for Chuffed (its `delay` is 0).
Providers get an optional `prefetch(urls, fetch)` hook; a failed prefetch
prints one line and the run falls back to single requests.
Links with only a name (`/project/aid-for-yazan`) are looked up by that
name, since Chuffed accepts slugs as ids. A single lookup that comes back
null with an "Invalid query" error now counts as not found, like a batch.

**Drop it:** Revert this commit; Chuffed goes back to one request per
campaign and name-only links go back to `invalid_url`.

## 22. Dead links stop syncing (2026-10-15)

**What:** When a fundraiser's page is not found on 3 runs in a row (status
`not_found`), the sync also sets its `auto_sync` to false, so it stops
asking the platform every night. The run prints
`<id>: not_found, sync turned off` (or `would be turned off` in a dry run).
The website's "Fundraiser page not found" data check lists these rows, and
staff can tick automatic updates again on the campaign's edit page.
`grants.sql` now lets the `bfep_sync` role update `auto_sync`.

**Drop it:** Revert this commit; dead links keep being checked every night.
