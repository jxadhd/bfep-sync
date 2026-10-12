"""Tests for bfep backup: request signing, the request ledger, the keep/delete
plan, and whole runs against a fake R2 server on localhost.

Nothing here talks to Cloudflare, a database or Docker: "bfep backup" runs
with a fake docker command that stands in for pg_dump.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

from bfep_backup import __main__ as cli
from bfep_backup.r2 import BudgetError, Ledger, plan, sign

ROOT = pathlib.Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc
GB = 10**9


class SigningTest(unittest.TestCase):

    def test_aws_documented_example(self):
        # "GET Object" example from the AWS Signature Version 4 documentation.
        h = sign('GET', 'examplebucket.s3.amazonaws.com', '/test.txt', {}, {'Range': 'bytes=0-9'},
                 hashlib.sha256(b'').hexdigest(), 'AKIAIOSFODNN7EXAMPLE',
                 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', dt.datetime(2013, 5, 24, tzinfo=UTC),
                 region='us-east-1')
        self.assertEqual(
            h['Authorization'],
            'AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, '
            'SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, '
            'Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41')


class LedgerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.now = dt.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
        self.ledger = Ledger(str(self.tmp / 'sub' / 'l.json'), {'A': 2, 'B': 1, 'free': 5},
                             clock=lambda: self.now)

    def test_refuses_past_the_limit_and_persists(self):
        self.ledger.spend('PutObject')
        self.ledger.spend('ListObjectsV2')
        with self.assertRaises(BudgetError):
            self.ledger.spend('PutObject')
        self.ledger.spend('GetObject')
        with self.assertRaises(BudgetError):
            self.ledger.spend('HeadObject')
        self.ledger.spend('DeleteObject')
        again = Ledger(self.ledger.path, self.ledger.limits, clock=lambda: self.now)
        self.assertEqual(again.counts(), {'A': 2, 'B': 1, 'free': 1})

    def test_new_month_starts_at_zero(self):
        self.ledger.spend('PutObject')
        self.ledger.spend('PutObject')
        self.now = dt.datetime(2026, 11, 1, tzinfo=UTC)
        self.ledger.spend('PutObject')
        self.assertEqual(self.ledger.counts()['A'], 1)

    def test_damaged_ledger_fails_closed(self):
        os.makedirs(self.tmp / 'sub')
        pathlib.Path(self.ledger.path).write_text('{not json')
        with self.assertRaises(BudgetError):
            self.ledger.spend('PutObject')
        self.assertEqual(pathlib.Path(self.ledger.path).read_text(), '{not json')


def obj(key, size):
    return {'key': key, 'size': size, 'modified': ''}


def name(day, hour=4):
    return f'bfep-db-202610{day:02d}T{hour:02d}0000Z.tar'


class PlanTest(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 31, 5, tzinfo=UTC)

    def run_plan(self, objects, size, max_bytes=8 * GB, keep_days=30, keep_min=3, new=None):
        return plan(objects, 'db/', new or name(31), size, self.NOW, max_bytes, 2 * GB,
                    keep_days, keep_min)

    def test_deletes_expired_but_keeps_the_newest(self):
        objects = [obj('db/' + name(d), GB // 10) for d in (1, 2, 3, 20, 25)]
        p = self.run_plan(objects, GB // 10, keep_days=10, keep_min=2)
        # 20 and 25 are protected by keep_min, 1-3 are older than 10 days.
        self.assertEqual(sorted(p.expired), ['db/' + name(d) for d in (1, 2, 3)])
        self.assertTrue(p.upload)
        p = self.run_plan(objects, GB // 10, keep_days=1, keep_min=5)
        self.assertEqual(p.expired, [])

    def test_makes_room_oldest_first(self):
        objects = [obj('db/' + name(d), 2 * GB) for d in (26, 27, 28, 29, 30)]  # 10 GB
        p = self.run_plan(objects, 2 * GB, max_bytes=8 * GB, keep_min=2)
        self.assertEqual(p.make_room, ['db/' + name(26), 'db/' + name(27)])
        self.assertEqual(p.total_after, 8 * GB)

    def test_refuses_rather_than_delete_protected_backups(self):
        objects = [obj('db/' + name(d), 3 * GB) for d in (29, 30)]
        p = self.run_plan(objects, 2 * GB, max_bytes=7 * GB, keep_min=2)
        self.assertFalse(p.upload)
        self.assertEqual(p.make_room, [])
        self.assertIn('does not fit', p.refused)
        self.assertEqual(p.total_after, 6 * GB)

    def test_other_objects_count_but_are_never_deleted(self):
        objects = [obj('photos/big.jpg', 6 * GB), obj('db/notes.txt', 1),
                   obj('db/' + name(1), GB // 2)]
        p = self.run_plan(objects, GB * 16 // 10, keep_days=365, keep_min=0)
        self.assertEqual(p.make_room, ['db/' + name(1)])
        p = self.run_plan(objects, 2 * GB, keep_days=365, keep_min=0)
        self.assertFalse(p.upload)
        self.assertNotIn('photos/big.jpg', p.deletes)

    def test_oversized_and_duplicate_backups_are_refused(self):
        self.assertIn('per-backup limit', self.run_plan([], 3 * GB).refused)
        self.assertIn('already', self.run_plan([obj('db/' + name(31), 1)], 1).refused)


class SettingsTest(unittest.TestCase):
    BASE = {'R2_ACCOUNT_ID': 'acct', 'R2_BUCKET': 'b', 'R2_ACCESS_KEY_ID': 'k',
            'R2_SECRET_ACCESS_KEY': 's'}

    def test_defaults_sit_inside_the_free_tier(self):
        s = cli.settings(self.BASE)
        self.assertEqual(s['endpoint'], 'https://acct.r2.cloudflarestorage.com')
        self.assertEqual(s['max_bytes'], 8 * GB)
        self.assertEqual(s['limits'], {'A': 1000, 'B': 1000, 'free': 1000})

    def test_limits_above_the_free_tier_are_rejected(self):
        for k, v in [('BACKUP_MAX_GB', '10'), ('BACKUP_MAX_CLASS_A', '1000000'),
                     ('BACKUP_MAX_CLASS_B', '5000000'), ('BACKUP_MAX_OBJECT_GB', '6'),
                     ('BACKUP_MAX_GB', 'lots'), ('BACKUP_KEEP_MIN', '0')]:
            with self.subTest(k=k, v=v), self.assertRaises(cli.ConfigError):
                cli.settings({**self.BASE, k: v})
        with self.assertRaises(cli.ConfigError):
            cli.settings({**self.BASE, 'R2_SECRET_ACCESS_KEY': ''})


class FakeR2(http.server.ThreadingHTTPServer):
    """Enough of R2's S3 API for these tests. Checks every request's signature."""

    def __init__(self):
        super().__init__(('127.0.0.1', 0), FakeR2Handler)
        self.objects = {}           # key -> bytes
        self.log = []               # (method, key)
        self.bad_signatures = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def endpoint(self):
        return f'http://127.0.0.1:{self.server_address[1]}'


class FakeR2Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def parse(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        query = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        auth = self.headers['Authorization']
        signed = auth.split('SignedHeaders=')[1].split(',')[0].split(';')
        hdrs = {h: self.headers[h] for h in signed if h not in ('host', 'x-amz-date',
                                                                 'x-amz-content-sha256')}
        when = dt.datetime.strptime(self.headers['x-amz-date'], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
        body = b''
        if self.command == 'PUT':
            body = self.rfile.read(int(self.headers['Content-Length']))
            if hashlib.sha256(body).hexdigest() != self.headers['x-amz-content-sha256']:
                self.server.bad_signatures += 1
        expect = sign(self.command, self.headers['host'], path, query, hdrs,
                      self.headers['x-amz-content-sha256'], 'key', 'secret', when)
        if expect['Authorization'] != auth:
            self.server.bad_signatures += 1
        _, bucket, key = (path + '/').split('/', 2)
        key = key[:-1]
        assert bucket == 'bucket', bucket
        self.server.log.append((self.command, key))
        return key, query, body

    def reply(self, status, body=b'', headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        key, query, _ = self.parse()
        if not key:
            prefix = query.get('prefix', '')
            items = ''.join(f'<Contents><Key>{k}</Key><Size>{len(v)}</Size>'
                            f'<LastModified>2026-10-01T00:00:00.000Z</LastModified></Contents>'
                            for k, v in sorted(self.server.objects.items()) if k.startswith(prefix))
            xml = ('<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   f'<IsTruncated>false</IsTruncated>{items}</ListBucketResult>')
            return self.reply(200, xml.encode())
        if key not in self.server.objects:
            return self.reply(404, b'<Error><Code>NoSuchKey</Code><Message>nope</Message></Error>')
        self.reply(200, self.server.objects[key])

    def do_PUT(self):
        key, _, body = self.parse()
        assert self.headers['x-amz-storage-class'] == 'STANDARD'
        self.server.objects[key] = body
        self.reply(200, headers=[('ETag', '"%s"' % hashlib.md5(body).hexdigest())])

    def do_DELETE(self):
        key, _, _ = self.parse()
        self.server.objects.pop(key, None)
        self.reply(204)


class UploadTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.r2 = FakeR2()
        self.addCleanup(self.r2.server_close)
        self.addCleanup(self.r2.shutdown)
        self.env = {'R2_ENDPOINT': self.r2.endpoint, 'R2_BUCKET': 'bucket', 'R2_ACCESS_KEY_ID': 'key',
                    'R2_SECRET_ACCESS_KEY': 'secret', 'BACKUP_LEDGER': str(self.tmp / 'ledger.json'),
                    'BACKUP_KEEP_MIN': '2', 'BACKUP_KEEP_DAYS': '5'}

    def main(self, *argv, **env):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv), {**self.env, **env})
        return rc, out.getvalue() + err.getvalue()

    def backup(self, ts, data=b'x' * 1000):
        p = self.tmp / f'bfep-db-{ts}.tar'
        p.write_bytes(data)
        return str(p)

    def test_upload_deletes_expired_then_puts(self):
        old = {f'db/bfep-db-2020010{d}T000000Z.tar': b'o' * 10 for d in (1, 2, 3)}
        self.r2.objects.update(old)
        self.r2.objects['other/keep.txt'] = b'k'
        f = self.backup('20261012T040000Z', b'new data')
        rc, out = self.main('upload', f)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.r2.objects['db/bfep-db-20261012T040000Z.tar'], b'new data')
        self.assertNotIn('db/bfep-db-20200101T000000Z.tar', self.r2.objects)
        self.assertIn('db/bfep-db-20200102T000000Z.tar', self.r2.objects)   # keep_min
        self.assertIn('other/keep.txt', self.r2.objects)
        self.assertEqual([m for m, _ in self.r2.log], ['GET', 'DELETE', 'PUT'])
        self.assertEqual(self.r2.bad_signatures, 0)
        self.assertIn('Class A 2/1000', out)

    def test_dry_run_only_lists(self):
        rc, out = self.main('upload', '--dry-run', self.backup('20261012T040000Z'))
        self.assertEqual((rc, self.r2.objects), (0, {}), out)
        self.assertEqual(self.r2.log, [('GET', '')])

    def test_refusals_send_no_upload(self):
        self.r2.objects['db/bfep-db-20261011T040000Z.tar'] = b'y' * 19_999_500
        rc, out = self.main('upload', self.backup('20261012T040000Z'), BACKUP_MAX_GB='0.02')
        self.assertEqual(rc, 6, out)
        self.assertIn('does not fit', out)
        self.assertNotIn('PUT', [m for m, _ in self.r2.log])

        self.r2.log.clear()
        rc, out = self.main('upload', self.backup('20261013T040000Z'), BACKUP_MAX_CLASS_A='2')
        self.assertEqual(rc, 6, out)   # the earlier list used 1, this list the 2nd
        self.assertIn('PutObject refused', out)
        self.assertEqual(self.r2.log, [('GET', '')])

    def test_list_and_fetch(self):
        self.r2.objects['db/bfep-db-20261012T040000Z.tar'] = b'payload'
        rc, out = self.main('list')
        self.assertEqual(rc, 0, out)
        self.assertIn('1 backup(s)', out)
        rc, out = self.main('fetch', 'bfep-db-20261012T040000Z.tar', str(self.tmp))
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.tmp / 'bfep-db-20261012T040000Z.tar').read_bytes(), b'payload')
        rc, out = self.main('fetch', 'bfep-db-20261099T040000Z.tar', str(self.tmp))
        self.assertEqual(rc, 1, out)
        self.assertFalse(list(self.tmp.glob('*.part')))
        self.assertEqual(self.main('fetch', '../etc/passwd', str(self.tmp))[0], 4)
        self.assertEqual(self.main('usage')[1].count('Class B 2/1000'), 1)


FAKE_DOCKER = r'''#!/usr/bin/env bash
# Stands in for "docker run ... postgres:18-alpine <tool> ...".
while [[ $# -gt 0 && "$1" != pg_dump && "$1" != pg_restore && "$1" != psql ]]; do shift; done
case "$1" in
  pg_dump) printf 'PGDMP fake dump of %s\n' "${@: -1}" ;;
  pg_restore) grep -q '^PGDMP' && echo '3; 0 1 TABLE DATA public t data' ;;
  psql) echo 'bfep_backup on db' ;;
esac
'''


@unittest.skipUnless(all(shutil.which(t) for t in ('bash', 'flock', 'tar', 'sha256sum')),
                     'needs bash, flock, tar and sha256sum')
class CommandTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.r2 = FakeR2()
        self.addCleanup(self.r2.server_close)
        self.addCleanup(self.r2.shutdown)
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        (bindir / 'docker').write_text(FAKE_DOCKER)
        (bindir / 'docker').chmod(0o755)
        svc = self.tmp / 'pg_service.conf'
        svc.write_text('[drupal]\nhost=x\n[bfdb]\nhost=x\n')
        svc.chmod(0o600)
        self.local = self.tmp / 'local'
        self.envfile = self.tmp / 'backup.env'
        self.envfile.write_text(
            f'R2_ENDPOINT={self.r2.endpoint}\nR2_BUCKET=bucket\nR2_ACCESS_KEY_ID=key\n'
            f'R2_SECRET_ACCESS_KEY=secret\nBACKUP_LEDGER={self.tmp}/ledger.json\n'
            f'PG_SERVICE_FILE={svc}\nBACKUP_LOCAL_DIR={self.local}\nBACKUP_LOCAL_KEEP=1\n')
        self.envfile.chmod(0o600)
        self.env = {**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
                    'ENV_FILE': str(self.envfile), 'RUN_LOCK': str(self.tmp / 'run.lock')}

    def bfep(self, *args, **env):
        return subprocess.run([str(ROOT / 'bin' / 'bfep'), 'backup', *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def test_run_dumps_packs_and_uploads(self):
        r = self.bfep()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [(key, data)] = self.r2.objects.items()
        self.assertRegex(key, r'^db/bfep-db-\d{8}T\d{6}Z\.tar$')
        tar = self.tmp / 'got.tar'
        tar.write_bytes(data)
        listing = subprocess.run(['tar', '-tf', str(tar)], capture_output=True, text=True).stdout
        self.assertEqual(sorted(listing.split()), ['SHA256SUMS', 'bfdb.dump', 'drupal.dump'])
        self.assertEqual(len(list(self.local.glob('bfep-db-*.tar'))), 1)
        self.assertFalse(list(self.local.glob('.work-*')))

    def test_dry_run_uploads_and_keeps_nothing(self):
        r = self.bfep(DRY_RUN='1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Dry run', r.stdout)
        self.assertEqual(self.r2.objects, {})
        self.assertEqual(self.r2.log, [('GET', '')])
        self.assertFalse(list(self.local.glob('bfep-db-*')))

    def test_refused_upload_keeps_the_local_copy(self):
        with open(self.envfile, 'a') as f:
            f.write('BACKUP_MAX_CLASS_A=1\n')   # the listing uses it, so the upload is refused
        r = self.bfep()
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(self.r2.objects, {})
        self.assertEqual(len(list(self.local.glob('bfep-db-*.tar'))), 1)

    def test_open_settings_file_is_refused(self):
        self.envfile.chmod(0o644)
        r = self.bfep('usage')
        self.assertEqual(r.returncode, 4)
        self.assertIn('chmod 600', r.stderr)
        self.assertEqual(self.r2.log, [])

    def test_encrypts_when_a_passphrase_is_set(self):
        if not shutil.which('gpg'):
            self.skipTest('needs gpg')
        pw = self.tmp / 'pw'
        pw.write_text('correct horse\n')
        with open(self.envfile, 'a') as f:
            f.write(f'BACKUP_PASSPHRASE_FILE={pw}\n')
        r = self.bfep(GNUPGHOME=str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [key] = self.r2.objects
        self.assertTrue(key.endswith('.tar.gpg'))
        self.assertNotIn(b'PGDMP', self.r2.objects[key])


if __name__ == '__main__':
    unittest.main()
