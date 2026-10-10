# Putting the new sync on butterfly

Written for the server as it is today (2026-10-10):

- The old sync is in `/opt/bfep-sync` (`sync.py` + `gofundme.py`).
- `bfep-sync.timer` runs `bfep-sync.service` nightly at 02:30 Pacific/Auckland,
  as root, under `flock -n /run/bfep-sync.lock`, with settings from
  `/etc/bfep-sync.env`. After a run that changed figures it calls
  `docker exec -w /opt/drupal drupal vendor/bin/drush cache:tag bfep:campaigns`.
- The folder also holds `chuffed-wip/` and `gogetfunding-wip/`. Step 5
  carries them over.

The new `sync.py` takes the same options and reads the same settings, so the
plan is to **keep the service, timer and settings file exactly as they are and
swap the folder**. You unpack the new version beside the old one, test it
there, then swap the folders. Rollback is swapping them back.

Run everything as root on butterfly, and not close to 02:30 NZ time.
Nothing here was run against production by Claude.

## 1. Back up

```sh
STAMP=$(date +%F)
cp -a /opt/bfep-sync /opt/bfep-sync.bak-$STAMP
mkdir -p /root/bfep-sync-bak-$STAMP
cp -a /etc/systemd/system/bfep-sync.service /etc/systemd/system/bfep-sync.timer \
      /etc/bfep-sync.env /root/bfep-sync-bak-$STAMP/
```

Back up the columns the sync writes, so figures can be put back if a run goes
wrong. This uses the sync's own login and Python, so it needs no psql:

```sh
( set -a; . /etc/bfep-sync.env; set +a
  python3 -c "
import psycopg2, sys
c = psycopg2.connect().cursor()
c.copy_expert('''COPY (SELECT id, donated_amount, goal_amount, currency_code, last_checked_at,
  last_success_at, sync_status, consecutive_failures FROM campaign_fundraisers) TO STDOUT CSV HEADER''', sys.stdout)
" ) > /root/bfep-sync-bak-$STAMP/campaign_fundraisers.csv
wc -l /root/bfep-sync-bak-$STAMP/campaign_fundraisers.csv
```

The line count should be your number of fundraisers plus one.

## 2. Unpack the new version beside the old one

Copy `bfep-sync-release.tar.gz` to root's home folder on the server, then:

```sh
mkdir /opt/bfep-sync.new
tar -xzf ~/bfep-sync-release.tar.gz -C /opt/bfep-sync.new
cd /opt/bfep-sync.new && python3 -m unittest discover -s tests
```

It should end with `OK (skipped=9)`. The 9 skipped tests are the database
tests, which only run against a scratch server. It uses the same system
`python3` and `psycopg2` the old script already uses, so there is nothing to
install.

## 3. Dry run, and compare with the old script

```sh
cd /opt/bfep-sync.new
( set -a; . /etc/bfep-sync.env; set +a
  OLD_PYTHON=python3 NEW_PYTHON=python3 sh deploy/compare-dry-run.sh /opt/bfep-sync 5 )
```

This dry-runs the old and new versions on the same 5 campaigns and writes
nothing. It takes about a minute. `SAME` means they would do exactly the same
thing. If it says `DIFFERENT`, a raised amount that grew a little between the
two runs is a donation arriving, which is fine. Anything else, or
`parse_error` on every campaign, send me the output.

## 4. One small real run with the new version

This is the service's own command with `--limit 5`, run from the new folder:

```sh
cd /opt/bfep-sync.new
( set -a; . /etc/bfep-sync.env; set +a
  flock -n /run/bfep-sync.lock python3 sync.py --apply --limit 5 \
    --after "/usr/bin/docker exec -w /opt/drupal drupal vendor/bin/drush cache:tag bfep:campaigns" )
```

`flock` stops this overlapping a timer run. Open those 5 campaigns in the
admin: "Last sync" on the edit form should show the new time and status, and
the public pages the new figures.

## 5. Swap the folders

```sh
mv /opt/bfep-sync /opt/bfep-sync.old
mv /opt/bfep-sync.new /opt/bfep-sync
cp -a /opt/bfep-sync.old/chuffed-wip /opt/bfep-sync.old/gogetfunding-wip /opt/bfep-sync/
```

The service and timer don't change. To watch a full run now rather than
waiting for 02:30:

```sh
systemctl start bfep-sync.service
journalctl -u bfep-sync -n 60 --no-pager
```

From now on the new version runs every night. `journalctl -u bfep-sync --since yesterday`
shows the last run, and the site's status report and admin dashboard show sync
freshness and campaigns with sync problems.

## 6. Optional extras, any time later

None of these is needed for the swap, and each is separate.

- **Restricted database login.** `deploy/grants.sql` creates a `bfep_sync`
  login that can only read and update the fundraiser columns the sync uses.
  Run it as `postgres` (`psql -d <db> -v ON_ERROR_STOP=1 -f deploy/grants.sql`,
  then `\password bfep_sync`), and put the new user and password in
  `/etc/bfep-sync.env`.
- **Retries.** The new version retries a timeout or a 5xx once per campaign
  (`--retries 1`, the default). Add `--retries 0` to the service's command to
  turn that off.

Your `--after` already clears everything that shows figures: campaign, home,
listing and country pages all carry the `bfep:campaigns` tag.
`deploy/bfep-sync.service`, `deploy/bfep-sync.timer` and `deploy/after-sync.sh`
are only for a fresh install on another server. Don't copy them over your existing units.

## Rollback

```sh
mv /opt/bfep-sync /opt/bfep-sync.failed
mv /opt/bfep-sync.old /opt/bfep-sync
```

If you changed the units or the settings file in step 6, copy them back from
`/root/bfep-sync-bak-<date>/` and run `systemctl daemon-reload`.

To put figures back from the step 1 backup:

```sh
( set -a; . /etc/bfep-sync.env; set +a
  python3 -c "
import psycopg2, sys
conn = psycopg2.connect(); c = conn.cursor()
c.execute('''CREATE TEMP TABLE cf_backup (id int, donated_amount numeric, goal_amount numeric,
  currency_code text, last_checked_at timestamptz, last_success_at timestamptz,
  sync_status text, consecutive_failures int)''')
c.copy_expert('COPY cf_backup FROM STDIN CSV HEADER', open(sys.argv[1]))
c.execute('''UPDATE campaign_fundraisers cf SET donated_amount = b.donated_amount,
  goal_amount = b.goal_amount, currency_code = b.currency_code,
  last_checked_at = b.last_checked_at, last_success_at = b.last_success_at,
  sync_status = b.sync_status, consecutive_failures = b.consecutive_failures
  FROM cf_backup b WHERE b.id = cf.id''')
print(c.rowcount, 'rows restored'); conn.commit()
" /root/bfep-sync-bak-<date>/campaign_fundraisers.csv )
```

Then clear the site cache with drush.

After a week of nightly runs that look right, delete `/opt/bfep-sync.old` and
`/opt/bfep-sync.bak-<date>`.
