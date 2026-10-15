"""Database tests: the real SQL, grants and lock against a throwaway PostgreSQL.

Skipped unless BFEP_SYNC_TEST_DSN points at a PostgreSQL server you can
create databases and roles on (a local scratch server, never production):

    BFEP_SYNC_TEST_DSN='host=/tmp port=5433 user=postgres' \\
        python -m unittest tests.test_postgres -v

Each test class creates its own database named bfep_sync_test_<random> and
drops only that database afterwards. The roles `data` (schema owner) and
`bfep_sync` (from deploy/grants.sql) are created if missing and left in place.
The schema mirrors the production columns the sync reads and writes.

Platform pages come from tests/fixtures via a patched urllib, so nothing
touches the network, and sleeps are patched out.
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import re
import runpy
import sys
import tempfile
import unittest
import urllib.error
import uuid
from decimal import Decimal
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get('BFEP_SYNC_TEST_DSN')
try:
    import psycopg2
    from psycopg2.extensions import make_dsn, parse_dsn
except ImportError:  # pragma: no cover
    psycopg2 = None

from bfep_sync.__main__ import main  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'
OK_PAGE = (FIXTURES / 'gofundme_ok.html').read_text()  # 12345.5 raised, 50000 goal, USD

SCHEMA = """
CREATE TABLE campaigns (
  id serial PRIMARY KEY,
  title text NOT NULL,
  internal_notes text,
  deleted_at timestamptz
);
CREATE TABLE campaign_fundraisers (
  id serial PRIMARY KEY,
  campaign_id int NOT NULL REFERENCES campaigns(id),
  platform_id int,
  url text,
  currency_code varchar(3),
  goal_amount numeric(12,2),
  donated_amount numeric(12,2),
  is_active boolean NOT NULL DEFAULT true,
  auto_sync boolean NOT NULL DEFAULT true,
  last_checked_at timestamptz,
  last_success_at timestamptz,
  sync_status text,
  consecutive_failures int NOT NULL DEFAULT 0
);
"""

# (id, url, currency, goal, raised, is_active, auto_sync, campaign deleted)
ROWS = [
    (1, 'https://www.gofundme.com/f/ok-same', 'USD', '50000', '12345.50', True, True, False),
    (2, 'https://www.gofundme.com/f/ok-up', 'USD', '50000', '10000', True, True, False),
    (3, 'gofund.me/abc123', 'GBP', '50000', '10000', True, True, False),        # label fixed (goal matches)
    (4, 'https://www.gofundme.com/f/mismatch', 'EUR', '9999', '10000', True, True, False),
    (5, 'https://www.gofundme.com/f/drop', 'USD', '50000', '20000', True, True, False),
    (6, 'https://www.gofundme.com/f/gone', 'USD', '1000', '10', True, True, False),
    (7, 'https://www.gofundme.com/f/broken', 'USD', '1000', '10', True, True, False),
    (8, 'https://www.gofundme.com/f/no-currency', None, None, '5', True, True, False),
    (9, 'https://www.gofundme.com/f/server-error', 'USD', '1000', '10', True, True, False),
    (10, 'https://evil.example/gofundme.com', 'USD', '1000', '10', True, True, False),   # invalid_url
    (11, 'https://www.gofundme.com/f/inactive', 'USD', '1', '1', False, True, False),
    (12, 'https://www.gofundme.com/f/manual', 'USD', '1', '1', True, False, False),
    (13, 'https://www.gofundme.com/f/deleted', 'USD', '1', '1', True, True, True),
    (14, 'https://www.justgiving.com/page/other', 'GBP', '1', '1', True, True, False),
]
SELECTED = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}


def http_error(url, code):
    return urllib.error.HTTPError(url, code, 'x', {}, io.BytesIO())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, 'full_url') else req
    if url.endswith('/gone'):
        raise http_error(url, 404)
    if url.endswith('/broken'):
        return FakeResponse(b'<html>no data</html>')
    if url.endswith('/server-error'):
        raise http_error(url, 503)
    return FakeResponse(OK_PAGE.encode())


@unittest.skipUnless(DSN and psycopg2, 'set BFEP_SYNC_TEST_DSN to a scratch PostgreSQL server')
class PostgresTestCase(unittest.TestCase):
    """Creates a fresh database per test, owned by `data`, with grants.sql applied."""

    def setUp(self):
        self.dbname = 'bfep_sync_test_' + uuid.uuid4().hex[:10]
        admin = psycopg2.connect(DSN)
        admin.autocommit = True
        with admin.cursor() as cur:
            for role in ('data', 'bfep_sync'):
                cur.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (role,))
                if not cur.fetchone():
                    cur.execute(f'CREATE ROLE {role} LOGIN')
            # A password so the tests also work over TCP (as in CI).
            cur.execute("ALTER ROLE bfep_sync PASSWORD 'bfep_sync_test'")
            cur.execute(f'CREATE DATABASE {self.dbname} OWNER data')
        admin.close()
        self.addCleanup(self._drop)

        conn = self.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('GRANT ALL ON SCHEMA public TO data')
            cur.execute('SET ROLE data')
            cur.execute(SCHEMA)
            for rid, url, ccy, goal, raised, active, auto, deleted in ROWS:
                cur.execute('INSERT INTO campaigns (id, title, deleted_at) VALUES (%s, %s, %s)',
                            (rid, f'Campaign {rid}', '2026-01-01' if deleted else None))
                cur.execute(
                    """INSERT INTO campaign_fundraisers (id, campaign_id, url, currency_code,
                         goal_amount, donated_amount, is_active, auto_sync)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (rid, rid, url, ccy, goal, raised, active, auto))
            cur.execute((ROOT / 'deploy' / 'grants.sql').read_text())
        conn.close()

        # The sync connects with PG* variables, as on the server, as bfep_sync.
        params = parse_dsn(DSN)
        env = {'PGDATABASE': self.dbname, 'PGUSER': 'bfep_sync', 'PGPASSWORD': 'bfep_sync_test',
               'SYNC_PROVIDERS': 'gofundme'}
        if 'host' in params:
            env['PGHOST'] = params['host']
        if 'port' in params:
            env['PGPORT'] = params['port']
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch('urllib.request.urlopen', fake_urlopen),
                        mock.patch('time.sleep', lambda s: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _drop(self):
        admin = psycopg2.connect(DSN)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS {self.dbname} WITH (FORCE)')
        admin.close()

    def connect(self, **kw):
        return psycopg2.connect(make_dsn(DSN, dbname=self.dbname, **kw))

    def table(self):
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("""SELECT id, currency_code, goal_amount, donated_amount, sync_status,
                                  consecutive_failures, last_checked_at IS NOT NULL,
                                  last_success_at IS NOT NULL
                           FROM campaign_fundraisers ORDER BY id""")
            rows = {r[0]: r[1:] for r in cur.fetchall()}
        conn.close()
        return rows

    def run_new(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()


class SyncAgainstPostgresTest(PostgresTestCase):

    def test_dry_run_selects_the_right_rows_and_writes_nothing(self):
        before = self.table()
        code, out, _ = self.run_new()
        self.assertEqual(code, 0)
        self.assertIn('DRY RUN: 10 campaigns', out)
        printed = {int(line.split(':')[0]) for line in out.splitlines() if line[:1].isdigit()}
        self.assertEqual(printed, SELECTED)
        self.assertEqual(self.table(), before)

    def test_apply_as_least_privilege_role(self):
        code, out, err = self.run_new('--apply')
        self.assertEqual(code, 0, err)
        t = self.table()
        # (currency, goal, raised, status, failures, checked, succeeded)
        self.assertEqual(t[1][:6], ('USD', Decimal('50000.00'), Decimal('12345.50'), 'ok', 0, True))
        self.assertEqual(t[2][2:4], (Decimal('12345.50'), 'ok'))
        self.assertEqual(t[3][0], 'USD')                       # label adopted
        self.assertEqual(t[4][0], 'USD')                       # stated currency adopted
        self.assertEqual(t[5][2:4], (Decimal('20000.00'), 'suspicious_drop'))
        self.assertEqual(t[6][3:5], ('not_found_pending', 1))
        self.assertEqual(t[7][3:5], ('parse_error', 1))
        self.assertEqual(t[8][0], 'USD')                       # empty currency filled in
        self.assertEqual(t[9][3:5], ('http_503', 0))
        self.assertEqual(t[10][3], 'invalid_url')
        for rid in (11, 12, 13, 14):
            self.assertIsNone(t[rid][3], f'row {rid} should not be touched')
        self.assertFalse(t[5][6], 'a held row must not get last_success_at')

    def test_not_found_becomes_final_after_three_runs(self):
        before = self.auto_sync()
        for _ in range(3):
            code, out, err = self.run_new('--apply')
        self.assertEqual(self.table()[6][3:5], ('not_found', 3))
        self.assertIn('6: not_found, sync turned off', out)
        self.assertEqual(self.auto_sync(), {**before, 6: False})

    def test_missing_auto_sync_grant_only_warns(self):
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute('REVOKE UPDATE (auto_sync) ON campaign_fundraisers FROM bfep_sync')
        conn.commit()
        conn.close()
        for _ in range(3):
            code, out, err = self.run_new('--apply')
            self.assertEqual(code, 0, err)
        self.assertEqual(self.table()[6][3:5], ('not_found', 3))
        self.assertIn('6: could not turn sync off', out)
        self.assertTrue(self.auto_sync()[6])

    def auto_sync(self):
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute('SELECT id, auto_sync FROM campaign_fundraisers ORDER BY id')
            rows = dict(cur.fetchall())
        conn.close()
        return rows

    def test_limit_takes_least_recently_checked_first(self):
        self.run_new('--apply', '--limit', '3')
        code, out, _ = self.run_new('--limit', '3')
        printed = [int(line.split(':')[0]) for line in out.splitlines() if line[:1].isdigit()]
        self.assertEqual(printed, [4, 5, 6])

    def test_role_cannot_read_other_columns_or_insert(self):
        conn = self.connect(user='bfep_sync', password='bfep_sync_test')
        cur = conn.cursor()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            cur.execute('SELECT internal_notes FROM campaigns')
        conn.rollback()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO campaign_fundraisers (campaign_id) VALUES (1)")
        conn.rollback()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            cur.execute('UPDATE campaign_fundraisers SET url = NULL WHERE id = 1')
        conn.close()

    def test_apply_waits_for_lock_but_dry_run_does_not(self):
        holder = self.connect()
        holder.cursor().execute('SELECT pg_advisory_lock(42633702)')
        try:
            code, _, err = self.run_new('--apply')
            self.assertEqual(code, 3)
            self.assertIn('another sync run', err)
            self.assertEqual(self.run_new()[0], 0)
        finally:
            holder.close()

    def test_after_hook_runs_only_when_something_changed(self):
        with tempfile.TemporaryDirectory() as d:
            marker = pathlib.Path(d) / 'ran'
            hook = f'touch {marker}'
            self.run_new('--limit', '3', '--after', hook)        # dry run: never
            self.assertFalse(marker.exists())
            self.run_new('--apply', '--limit', '1', '--after', hook)  # row 1 unchanged
            self.assertFalse(marker.exists())
            self.run_new('--apply', '--after', hook)
            self.assertTrue(marker.exists())

    def test_unreachable_database_exits_5(self):
        with mock.patch.dict(os.environ, {'PGDATABASE': 'bfep_sync_test_does_not_exist'}):
            code, _, err = self.run_new()
        self.assertEqual(code, 5)
        self.assertIn('database connection failed', err)


class ParityWithOriginalTest(PostgresTestCase):
    """The original sync.py and the new package, run against identical
    databases with identical pages, must print the same lines and leave
    identical rows (apart from timestamps)."""

    def run_original(self, *argv):
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['sync.py', *argv]), \
                mock.patch.object(sys, 'path', [str(ROOT / 'original'), *sys.path]), \
                contextlib.redirect_stdout(out):
            sys.modules.pop('gofundme', None)
            try:
                runpy.run_path(str(ROOT / 'original' / 'sync.py'), run_name='__main__')
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue()

    def snapshot_db(self):
        """A second database with the same rows, so both scripts start equal."""
        copy = self.dbname + '_orig'
        admin = psycopg2.connect(DSN)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE {copy} TEMPLATE {self.dbname} OWNER data')
        admin.close()

        def drop():
            a = psycopg2.connect(DSN)
            a.autocommit = True
            a.cursor().execute(f'DROP DATABASE IF EXISTS {copy} WITH (FORCE)')
            a.close()
        self.addCleanup(drop)
        return copy

    def test_same_output_and_same_rows(self):
        copy = self.snapshot_db()
        # The original script holds currency changes; SYNC_HOLD_CURRENCY=1 keeps that rule.
        hold = mock.patch.dict(os.environ, {'SYNC_HOLD_CURRENCY': '1'})
        hold.start()
        self.addCleanup(hold.stop)
        for argv in ((), ('--apply',), ('--apply',), ('--apply',)):
            new_code, new_out, _ = self.run_new('--retries', '0', *argv)
            with mock.patch.dict(os.environ, {'PGDATABASE': copy}):
                old_code, old_out = self.run_original(*argv)
            self.assertEqual(new_code, old_code)
            # The new lines add a reason to parse_error and error, e.g. "7: parse_error (no ...)",
            # and say when a dead link's sync is turned off (the original kept checking it).
            new_lines = [re.sub(r'^(\d+: (?:parse_)?error) \(.*\)$', r'\1', line) for line in new_out.splitlines()]
            new_lines = [re.sub(r'^(\d+: not_found), sync turned off$', r'\1', line) for line in new_lines]
            self.assertEqual(new_lines, old_out.splitlines())
        new_rows = self.table()
        self.dbname, orig = copy, self.dbname
        try:
            self.assertEqual(self.table(), new_rows)
        finally:
            self.dbname = orig


if __name__ == '__main__':
    unittest.main()
