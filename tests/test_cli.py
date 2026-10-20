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
                   'SITE_DEPLOY_LOCK', 'MODULES_DEST', 'THEMES_DEST', 'NO_UPDB', 'IMAGE_BUMP', 'NO_DEPLOY',
                   'CHECK', 'MINOR', 'DB_DUMP', 'APP_DIR', 'BACKUP_ROOT',
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

    def test_typos_get_a_hint_and_change_nothing(self):
        # Each of these used to run something, fail late, or print a bare error.
        cases = {
            ('synk',): 'Did you mean: bfep sync',
            ('help', 'updte'): 'Did you mean: bfep help update',
            ('sync', 'stauts'): 'Did you mean: bfep sync status',
            ('sync', 'apply'): 'Did you mean: bfep sync --apply',
            ('sync', 'log', '3d'): 'Did you mean: bfep sync log --since 3d',
            ('sync', 'log', '--sinse', '3d'): 'Did you mean: bfep sync log --since',
            ('sync', 'deploy', '--dry-run'): 'Did you mean: DRY_RUN=1 bfep sync deploy',
            ('sync', 'deploy', 'DRY_RUN=1'): 'Did you mean: DRY_RUN=1 bfep sync deploy',
            ('sync', 'rollback', 'now'): 'rollback takes no arguments',
            ('deploy', '--dryrun'): 'Did you mean: DRY_RUN=1 bfep deploy',
            ('deploy', 'NO_UPDB=1'): 'Did you mean: NO_UPDB=1 bfep deploy',
            ('deploy', 'a', 'b'): 'takes one branch',
            ('update', '--check'): 'Did you mean: CHECK=1 bfep update',
            ('update', 'check'): 'Did you mean: CHECK=1 bfep update',
            ('update', '--no-deploy'): 'Did you mean: NO_DEPLOY=1 bfep update',
            ('update', 'rollback'): 'Latest backups: none in',
        }
        env = {'APP_DIR': str(self.tmp / 'no-app'), 'BACKUP_ROOT': str(self.tmp / 'no-backups')}
        for args, hint in cases.items():
            with self.subTest(args=args):
                r = self.bfep(*args, env=env)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(hint, r.stderr)
        self.assertFalse((self.live / '.deployed').exists())
        for name in ('deploy', 'update'):
            r = self.bfep(name, '--help', env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f'Usage:  bfep {name}', r.stdout)

    def test_sync_options_are_checked_by_name(self):
        self.bfep('sync', 'deploy')
        r = self.bfep('sync', '--aply', '--provider', 'steunactie')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('usage: bfep sync ', r.stderr)
        self.assertIn('bfep sync: unknown option --aply (did you mean --apply?)', r.stderr)
        r = self.bfep('sync', '--provider', 'stuenactie')
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn('did you mean steunactie?', r.stderr)

    def test_accept_needs_fundraiser_ids(self):
        for args, message in ((('sync', 'accept'), 'accept needs the fundraiser IDs'),
                              (('sync', 'accept', '12', 'line5'), "not a fundraiser ID: 'line5'")):
            with self.subTest(args=args):
                r = self.bfep(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(message, r.stderr)
        self.assertIn('Did you mean: bfep sync accept', self.bfep('sync', 'acept').stderr)

    # bfep sync log

    def test_log_searches_the_last_day_unless_told_otherwise(self):
        fake = self.tmp / 'fakebin'
        fake.mkdir()
        (fake / 'journalctl').write_text('#!/bin/sh\necho "$@"\n')
        (fake / 'journalctl').chmod(0o755)
        env = {'PATH': f"{fake}:{os.environ['PATH']}", 'UNIT': 'bfep-sync'}
        base = '-u bfep-sync -n {} --no-pager -o cat'
        for args, expected in ((['log'], base.format(50) + ' --since -24h'),
                               (['log', '200'], base.format(200) + ' --since -24h'),
                               (['log', '--since', '3d'], base.format(50) + ' --since -3d'),
                               (['log', '--since=2026-10-01', '10'], base.format(10) + ' --since 2026-10-01'),
                               (['log', '--all'], base.format(50))):
            with self.subTest(args=args):
                r = self.bfep('sync', *args, env=env)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, expected), r.stderr)
        self.assertEqual(self.bfep('sync', 'log', '--since', env=env).returncode, 2)
        self.assertEqual(self.bfep('sync', 'log', 'lots', env=env).returncode, 2)

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

    # bfep update

    def test_update_rollback_restores_image_and_dockerfile(self):
        app, backups = self.tmp / 'app', self.tmp / 'backups'
        b = backups / '20261015-083023'
        b.mkdir(parents=True)
        app.mkdir()
        dockerfile = app / 'Dockerfile.drupal'
        dockerfile.write_text('FROM drupal:11.4.10-php8.5-apache-trixie\n')
        (b / 'Dockerfile').write_text('FROM drupal:11.4.8-php8.5-apache-trixie\n')
        (b / 'dockerfile-path').write_text(f'{dockerfile}\n')
        (b / 'image-name').write_text('local/drupal:bfep\n')
        fake = self.tmp / 'fakebin'
        fake.mkdir()
        calls = self.tmp / 'docker-calls'
        (fake / 'docker').write_text(
            '#!/bin/sh\n'
            f'echo "$*" >> {calls}\n'
            'case "$*" in *"drush status"*) echo Successful ;; *"which psql"*) exit 1 ;; esac\n'
            'exit 0\n')
        (fake / 'docker').chmod(0o755)
        env = {'PATH': f"{fake}:{os.environ['PATH']}", 'APP_DIR': str(app), 'BACKUP_ROOT': str(backups)}

        r = subprocess.run([str(ROOT / 'bin' / 'bfep'), 'update', 'rollback', '20261015-083023'],
                           env={**self.env, **env}, input='y\n', capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Rolled back to 20261015-083023', r.stdout)
        self.assertEqual(dockerfile.read_text(), 'FROM drupal:11.4.8-php8.5-apache-trixie\n')
        log = calls.read_text()
        self.assertIn('tag bfep-drupal-rollback:20261015-083023 local/drupal:bfep', log)
        self.assertIn('compose up -d --no-build --force-recreate drupal', log)
        self.assertIn('system.maintenance_mode 0', log)

        # A missing backup is refused before anything changes.
        r = self.bfep('update', 'rollback', '20991231-000000', env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('no complete backup', r.stderr)

    # Maintenance mode is never left on by a failed deploy or update

    def fake_docker(self, fail=''):
        """A docker that logs its calls; `fail` names the step that fails."""
        fake = self.tmp / 'fakebin'
        fake.mkdir(exist_ok=True)
        calls = self.tmp / 'docker-calls'
        (fake / 'docker').write_text(
            '#!/bin/sh\n'
            f'echo "$*" >> {calls}\n'
            'case "$*" in\n'
            '  "compose config -q") ;;\n'
            '  "compose config"*) printf "services:\\n  drupal:\\n    build:\\n      context: .\\n" ;;\n'
            '  *"drush status"*) echo Successful ;;\n'
            '  *"state:get system.maintenance_mode"*) echo 0 ;;\n'
            '  *"ls /opt/drupal/vendor/bin/drush"*) echo vendor/bin/drush ;;\n'
            f'  *"compose build"*) [ "{fail}" = build ] && exit 1 ;;\n'
            f'  *"updb -y"*) [ "{fail}" = updb ] && exit 1 ;;\n'
            'esac\n'
            'exit 0\n')
        (fake / 'docker').chmod(0o755)
        return {'PATH': f"{fake}:{os.environ['PATH']}"}, calls

    def site_repo(self):
        """A site checkout with one custom module, as bfep deploy expects."""
        origin, repo = self.tmp / 'site-origin', self.tmp / 'site'
        (origin / 'drupal' / 'modules' / 'custom' / 'bfep').mkdir(parents=True)
        (origin / 'drupal' / 'modules' / 'custom' / 'bfep' / 'bfep.info.yml').write_text('name: BFEP\n')
        self.git(origin, 'init', '-q', '-b', 'main')
        self.git(origin, 'add', '-A')
        self.git(origin, 'commit', '-q', '-m', 'site')
        self.git(self.tmp, 'clone', '-q', str(origin), str(repo))
        (self.tmp / 'modules').mkdir()
        return {'REPO': str(repo), 'MODULES_DEST': str(self.tmp / 'modules'),
                'THEMES_DEST': str(self.tmp / 'themes'), 'SITE_DEPLOY_LOCK': str(self.tmp / 'site.lock')}

    def test_deploy_turns_maintenance_off_when_updb_fails(self):
        docker, calls = self.fake_docker(fail='updb')
        r = self.bfep('deploy', env={**docker, **self.site_repo()})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Database updates did not finish', r.stderr)
        self.assertIn('Maintenance mode is off again', r.stderr)
        log = calls.read_text()
        self.assertIn('state:set system.maintenance_mode 0', log)
        self.assertLess(log.index('updb -y'), log.index('state:set system.maintenance_mode 0'))

    def test_deploy_leaves_maintenance_alone_when_updb_works(self):
        docker, calls = self.fake_docker()
        r = self.bfep('deploy', env={**docker, **self.site_repo()})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('maintenance_mode 0', calls.read_text())

    def update_env(self, docker):
        app = self.tmp / 'app'
        app.mkdir()
        dump = self.tmp / 'manual.dump'
        dump.write_text('dump')
        return {**docker, 'APP_DIR': str(app), 'BACKUP_ROOT': str(self.tmp / 'backups'),
                'NO_DEPLOY': '1', 'IMAGE_BUMP': '0', 'DB_DUMP': str(dump)}

    def test_update_failed_build_turns_maintenance_off(self):
        docker, calls = self.fake_docker(fail='build')
        r = self.bfep('update', env=self.update_env(docker))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('FAILED (exit 1) while: build', r.stderr)
        self.assertIn('Maintenance mode is off again', r.stderr)
        self.assertNotIn('bfep update rollback', r.stderr)
        log = calls.read_text()
        self.assertLess(log.index('maintenance_mode 1'), log.index('maintenance_mode 0'))
        self.assertNotIn('compose up', log)

    def test_update_failed_database_update_keeps_maintenance_on(self):
        docker, calls = self.fake_docker(fail='updb')
        r = self.bfep('update', env=self.update_env(docker))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('while: database updates', r.stderr)
        self.assertIn('still in maintenance mode', r.stderr)
        self.assertIn('bfep update rollback 20', r.stderr)
        self.assertNotIn('maintenance_mode 0', calls.read_text())

    def test_update_success_turns_maintenance_off(self):
        docker, calls = self.fake_docker()
        r = self.bfep('update', env={**self.update_env(docker), 'SITE_URL': 'http://127.0.0.1:9/'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Update complete', r.stdout)
        self.assertNotIn('FAILED', r.stderr)
        self.assertIn('maintenance_mode 0', calls.read_text())

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
