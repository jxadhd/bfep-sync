"""Tests for the bfep command and install.sh, against fake server folders.

Each test builds a throwaway "server" in a temporary directory: a git origin
holding this working tree, a checkout of it ($REPO), a live copy ($LIVE) with
an older version and a *-wip folder, a settings file, lock files and a fake
/usr/local/bin. Nothing outside that directory is touched, and nothing talks
to a real database, systemd or Docker.

Needs bash, git, rsync and flock; skipped otherwise.
"""
from __future__ import annotations

import fcntl
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ('bash', 'git', 'rsync', 'flock')
INNER = 'BFEP_CLI_TEST_INNER'   # set while a deploy runs the suite, to stop recursion
SCRIPT_SETTINGS = {'DRY_RUN', 'LIVE', 'PREV', 'REPO', 'ENV_FILE', 'RUN_LOCK', 'DEPLOY_LOCK',
                   'UNIT', 'BIN', 'KEEP_DIR', 'BFEP_LEGACY_DIR', 'BFEP_SYNC_TEST_DSN'}


@unittest.skipIf(os.environ.get(INNER), 'running inside a deploy under test')
@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), 'needs ' + ', '.join(TOOLS))
class BfepCommandTest(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='bfep-cli-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.origin, self.repo, self.live = t / 'origin', t / 'src', t / 'live'
        self.bin = t / 'bin'
        self.bin.mkdir()

        # Origin: this working tree (tracked and new files) as one commit.
        files = self.git_out(ROOT, 'ls-files', '-co', '--exclude-standard').split('\n')
        for f in filter(None, files):
            src = ROOT / f
            if src.is_file():
                dst = self.origin / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        self.git(self.origin, 'init', '-q', '-b', 'main')
        self.git(self.origin, 'add', '-A')
        self.git(self.origin, 'commit', '-q', '-m', 'test release')
        self.git(t, 'clone', '-q', str(self.origin), str(self.repo))

        # The live copy as installed by hand: an older version plus a WIP folder.
        (self.live / 'chuffed-wip').mkdir(parents=True)
        (self.live / 'chuffed-wip' / 'notes.txt').write_text('keep me')
        (self.live / 'sync.py').write_text('print("old version")\n')
        (self.live / 'old-only.txt').write_text('old')

        (t / 'env').write_text('PGHOST=/nonexistent\nPGDATABASE=none\nPGUSER=none\n')
        (self.bin / 'bfep-foo').write_text('#!/bin/sh\n# bfep-foo: an old standalone script\necho "foo ran with $*"\n')
        (self.bin / 'bfep-foo').chmod(0o755)

        # Settings the scripts read, which may be set in the calling shell (a
        # deploy runs this suite, possibly as DRY_RUN=1 bfep sync deploy).
        outer = {k: v for k, v in os.environ.items() if k not in SCRIPT_SETTINGS}
        self.env = {
            **outer, INNER: '1', 'LIVE': str(self.live), 'REPO': str(self.repo),
            'PREV': str(self.live.with_name('live.prev')),
            'ENV_FILE': str(t / 'env'), 'RUN_LOCK': str(t / 'run.lock'),
            'DEPLOY_LOCK': str(t / 'deploy.lock'), 'BFEP_LEGACY_DIR': str(self.bin),
            'GIT_CONFIG_GLOBAL': str(t / 'gitconfig'),
        }
        (t / 'gitconfig').write_text('[user]\n\tname = test\n\temail = test@example.org\n')

    # helpers

    def git(self, cwd, *args):
        subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.org', *args],
                       cwd=cwd, check=True, capture_output=True)

    def git_out(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    def bfep(self, *args, env=None):
        return subprocess.run([str(ROOT / 'bin' / 'bfep'), *args], env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=300)

    def hold(self, name):
        f = open(self.tmp / name, 'w')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)

    # bfep itself

    def test_help_lists_commands_and_old_scripts(self):
        r = self.bfep('help')
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in [p.name for p in (ROOT / 'commands').iterdir()] + ['foo']:
            self.assertRegex(r.stdout, rf'\n  {name} ')
        self.assertIn('an old standalone script', r.stdout)
        self.assertIn('bfep sync deploy', self.bfep('help', 'sync').stdout)

    def test_falls_back_to_old_script(self):
        r = self.bfep('foo', 'a', 'b')
        self.assertEqual((r.returncode, r.stdout), (0, 'foo ran with a b\n'))

    def test_rejects_unknown_and_odd_names(self):
        self.assertEqual(self.bfep('nope').returncode, 2)
        self.assertEqual(self.bfep('../bin/bfep').returncode, 2)
        self.assertEqual(self.bfep('help', '../x').returncode, 2)

    # bfep sync deploy / rollback

    def test_deploy_keeps_wip_and_previous_copy(self):
        r = self.bfep('sync', 'deploy')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Deployed', r.stdout)
        self.assertTrue((self.live / 'bfep_sync' / 'runner.py').is_file())
        self.assertTrue(os.access(self.live / 'bin' / 'bfep', os.X_OK))
        self.assertEqual((self.live / 'chuffed-wip' / 'notes.txt').read_text(), 'keep me')
        self.assertFalse((self.live / 'old-only.txt').exists())
        self.assertFalse((self.live / '.git').exists())
        self.assertIn('test release', (self.live / '.deployed').read_text())
        prev = self.live.with_name('live.prev')
        self.assertEqual((prev / 'sync.py').read_text(), 'print("old version")\n')

    def test_dry_run_touches_nothing(self):
        r = self.bfep('sync', 'deploy', env={'DRY_RUN': '1'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Dry run complete', r.stdout)
        self.assertEqual((self.live / 'sync.py').read_text(), 'print("old version")\n')
        self.assertFalse(self.live.with_name('live.prev').exists())

    def test_rollback_swaps_and_can_be_undone(self):
        self.bfep('sync', 'deploy')
        r = self.bfep('sync', 'rollback')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.live / 'sync.py').read_text(), 'print("old version")\n')
        self.bfep('sync', 'rollback')
        self.assertTrue((self.live / 'bfep_sync').is_dir())

    def test_refuses_while_a_sync_is_running(self):
        self.hold('run.lock')
        r = self.bfep('sync', 'deploy')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('sync run is in progress', r.stderr)
        self.assertEqual((self.live / 'sync.py').read_text(), 'print("old version")\n')

    def test_refuses_uncommitted_changes(self):
        (self.repo / 'README.md').write_text('edited on the server')
        r = self.bfep('sync', 'deploy')
        self.assertIn('uncommitted changes', r.stderr)

    def test_failing_tests_block_the_deploy(self):
        (self.origin / 'tests' / 'test_broken.py').write_text(
            'import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail()\n')
        self.git(self.origin, 'add', '-A')
        self.git(self.origin, 'commit', '-q', '-m', 'broken')
        r = self.bfep('sync', 'deploy')
        self.assertIn('tests failed; nothing deployed', r.stderr)
        self.assertEqual((self.live / 'sync.py').read_text(), 'print("old version")\n')

    def test_deploys_a_tag(self):
        self.git(self.origin, 'tag', 'v1.0')
        r = self.bfep('sync', 'deploy', 'v1.0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('detached', r.stderr.lower())
        r = self.bfep('sync', 'deploy', 'no-such-ref')
        self.assertIn("no branch, tag or commit called 'no-such-ref'", r.stderr)

    # bfep sync (running it)

    def test_run_reads_settings_and_reports_database_errors(self):
        self.bfep('sync', 'deploy')
        r = self.bfep('sync', '--limit', '1')
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn('database connection failed', r.stderr)

    def test_apply_waits_for_the_timer_run(self):
        self.bfep('sync', 'deploy')
        self.hold('run.lock')
        r = self.bfep('sync', '--apply')
        self.assertIn('sync run is in progress', r.stderr)
        self.assertEqual(self.bfep('sync', '--limit', '1').returncode, 5)   # dry runs don't wait

    # install.sh

    def test_install_links_bfep_and_wraps_old_scripts(self):
        old = self.bin / 'bfep-sync'
        old.write_text('#!/bin/sh\necho original sync\n')
        old.chmod(0o755)
        keep = self.tmp / 'replaced'
        env = {**self.env, 'BIN': str(self.bin), 'KEEP_DIR': str(keep), 'BFEP_INSTALL_ANY_USER': '1'}
        r = subprocess.run([str(self.repo / 'install.sh')], env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(os.readlink(self.bin / 'bfep'), str(self.live / 'bin' / 'bfep'))
        self.assertIn('exec bfep sync "$@"', old.read_text())
        self.assertEqual((keep / 'bfep-sync').read_text(), '#!/bin/sh\necho original sync\n')
        self.assertEqual((self.bin / 'bfep-foo').read_text().count('old standalone'), 1)   # not ours: untouched
        # The help shows "sync" once: the wrapper is not listed as an old script.
        self.assertEqual(r.stdout.count('\n  sync '), 1)
        # Running it again changes nothing.
        r = subprocess.run([str(self.repo / 'install.sh')], env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('now runs', r.stdout)


if __name__ == '__main__':
    unittest.main()
