"""Tests for lib/drupal_image.py, the base image update in `bfep update`.

Docker Hub is never contacted: every test passes its own tag list.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('drupal_image', ROOT / 'lib' / 'drupal_image.py')
di = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(di)

V = '-php8.5-apache-trixie'
D = {n: 'sha256:' + c * 64 for n, c in (('11.4.8', 'a'), ('11.4.9', 'b'), ('11.4.10', 'c'),
                                         ('11.3.19', 'd'), ('11.5.0', 'e'), ('12.0.0', 'f'))}
TAGS = ([{'name': n + V, 'digest': d} for n, d in D.items()]
        + [{'name': '11.4.11-php8.4-apache-trixie', 'digest': 'sha256:' + '9' * 64},
           {'name': '11.4' + V, 'digest': D['11.4.10']},
           {'name': '11.4.12' + V}])   # no digest: skipped


class BumpTest(unittest.TestCase):

    def dockerfile(self, first_line: str) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp(prefix='bfep-img-'))
        p = d / 'Dockerfile.drupal'
        p.write_text(first_line + '\n\nWORKDIR /opt/drupal\nRUN composer require drush/drush\n')
        return p

    def test_moves_to_newest_patch_in_same_minor(self):
        p = self.dockerfile(f"FROM drupal:11.4.8{V}@{D['11.4.8']}")
        msg = di.bump(str(p), tags=TAGS)
        self.assertIn('11.4.8' + V + ' -> 11.4.10' + V, msg)
        lines = p.read_text().split('\n')
        self.assertEqual(lines[0], f"FROM drupal:11.4.10{V}@{D['11.4.10']}")
        self.assertEqual(lines[2:], ['WORKDIR /opt/drupal', 'RUN composer require drush/drush', ''])

    def test_minor_allows_next_minor_but_never_next_major(self):
        p = self.dockerfile(f'FROM drupal:11.4.8{V}')
        di.bump(str(p), minor=True, tags=TAGS)
        self.assertEqual(p.read_text().split('\n')[0], f"FROM drupal:11.5.0{V}@{D['11.5.0']}")

    def test_check_changes_nothing(self):
        p = self.dockerfile(f"FROM drupal:11.4.8{V}@{D['11.4.8']}")
        before = p.read_text()
        self.assertTrue(di.bump(str(p), check=True, tags=TAGS).startswith('would move'))
        self.assertEqual(p.read_text(), before)

    def test_already_newest(self):
        p = self.dockerfile(f"FROM drupal:11.4.10{V}@{D['11.4.10']}")
        before = p.read_text()
        self.assertIn('already the newest', di.bump(str(p), tags=TAGS))
        self.assertEqual(p.read_text(), before)

    def test_rebuilt_tag_gets_new_digest(self):
        p = self.dockerfile(f"FROM drupal:11.4.10{V}@sha256:{'0' * 64}")
        self.assertIn('newer build', di.bump(str(p), tags=TAGS))
        self.assertIn(D['11.4.10'], p.read_text())

    def test_keeps_registry_prefix_and_stage_name(self):
        p = self.dockerfile(f'FROM docker.io/library/drupal:11.4.9{V} AS base')
        di.bump(str(p), tags=TAGS)
        self.assertEqual(p.read_text().split('\n')[0],
                         f"FROM docker.io/library/drupal:11.4.10{V}@{D['11.4.10']} AS base")

    def test_digest_only_is_looked_up_and_gets_its_tag(self):
        # Dependabot's form in the site repo; 11.4.8 has shorter alias tags too.
        tags = TAGS + [{'name': '11.4.8', 'digest': D['11.4.8']},
                       {'name': '11.4.8-apache', 'digest': D['11.4.8']}]
        p = self.dockerfile(f"FROM drupal@{D['11.4.8']}")
        msg = di.bump(str(p), tags=tags)
        self.assertIn(f"drupal@{D['11.4.8'][:19]}... (drupal:11.4.8{V}) -> 11.4.10{V}", msg)
        self.assertEqual(p.read_text().split('\n')[0], f"FROM drupal:11.4.10{V}@{D['11.4.10']}")

    def test_digest_only_already_newest_still_gets_its_tag(self):
        p = self.dockerfile(f"FROM drupal@{D['11.4.10']}")
        self.assertIn('tagged FROM line', di.bump(str(p), check=True, tags=TAGS))
        self.assertEqual(p.read_text().split('\n')[0], f"FROM drupal@{D['11.4.10']}")
        di.bump(str(p), tags=TAGS)
        self.assertEqual(p.read_text().split('\n')[0], f"FROM drupal:11.4.10{V}@{D['11.4.10']}")

    def test_unknown_digest_explains_what_to_do(self):
        p = self.dockerfile(f"FROM drupal@sha256:{'0' * 64}")
        before = p.read_text()
        with self.assertRaises(di.Unsupported) as e:
            di.bump(str(p), tags=TAGS)
        self.assertIn('IMAGE_BUMP=0 bfep update', str(e.exception))
        self.assertIn('FROM drupal:11.4.10-php8.5-apache-trixie', str(e.exception))
        self.assertEqual(p.read_text(), before)

    def test_other_images_are_refused(self):
        for line in ('FROM drupal:11.4-php8.5-apache-trixie', 'FROM drupal', 'FROM php:8.5-apache'):
            p = self.dockerfile(line)
            before = p.read_text()
            with self.assertRaises(di.Unsupported, msg=line):
                di.bump(str(p), tags=TAGS)
            self.assertEqual(p.read_text(), before)

    def test_no_tags_for_series(self):
        p = self.dockerfile('FROM drupal:10.6.1-php8.3-apache-bookworm')
        with self.assertRaises(di.Unsupported):
            di.bump(str(p), tags=TAGS)


class DockerfileTest(unittest.TestCase):

    def config(self, services: dict) -> str:
        f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        json.dump({'services': services}, f)
        f.close()
        return f.name

    def test_context_and_dockerfile(self):
        cfg = self.config({'drupal': {'build': {'context': '/opt/docker/drupal',
                                                'dockerfile': 'Dockerfile.drupal'}}})
        self.assertEqual(di.dockerfile(cfg, 'drupal'), '/opt/docker/drupal/Dockerfile.drupal')

    def test_default_dockerfile_name(self):
        cfg = self.config({'drupal': {'build': {'context': '/srv/app'}}})
        self.assertEqual(di.dockerfile(cfg, 'drupal'), '/srv/app/Dockerfile')

    def test_service_without_build(self):
        cfg = self.config({'drupal': {'image': 'drupal:11'}})
        with self.assertRaises(di.Unsupported):
            di.dockerfile(cfg, 'drupal')


def fake_fetch(names):
    """A hub_tags stand-in serving the given tag names by prefix."""
    def fetch(prefix):
        return [{'name': n, 'digest': 'sha256:' + '1' * 64} for n in names if n.startswith(prefix)]
    return fetch


class NewerTest(unittest.TestCase):

    def dockerfile(self, first_line: str) -> pathlib.Path:
        d = pathlib.Path(tempfile.mkdtemp(prefix='bfep-img-'))
        p = d / 'Dockerfile.drupal'
        p.write_text(first_line + '\nRUN composer require \\\n  "drupal/honeypot:^2.2" \\\n'
                     '  "drupal/oidc:^2.3.0" \\\n  "drush/drush:^13.7.6"\n')
        return p

    def releases(self, name):
        return {
            'drupal/honeypot': di.expand([{'version': '2.3.0', 'require': {'drupal/core': '^11 || ^12'}},
                                          {'version': '2.2.2', 'require': {'drupal/core': '^10.3 || ^11'}}]),
            'drupal/oidc': di.expand([{'version': '3.0.0-beta1', 'require': {'drupal/core': '^12'}},
                                      {'version': '2.3.0', 'require': {'drupal/core': '^10 || ^11'}}]),
        }[name]

    def test_nothing_newer(self):
        p = self.dockerfile(f'FROM drupal:11.4.10{V}')
        self.assertEqual(di.newer(str(p), fetch=fake_fetch(['11.4.10' + V])), '')

    def test_digest_only_reads_the_version_from_docker_hub(self):
        p = self.dockerfile(f"FROM drupal@{D['11.4.10']}")
        msg = di.newer(str(p), fetch=fake_fetch(['11.4.10' + V, '11.5.0' + V]), tags=TAGS)
        self.assertIn('Drupal 11.5.0 is out; this update stays on 11.4.x', msg)

    def test_new_minor_without_minor_flag(self):
        p = self.dockerfile(f'FROM drupal:11.4.10{V}')
        msg = di.newer(str(p), fetch=fake_fetch(['11.4.10' + V, '11.5.0' + V, '11.5.1' + V]))
        self.assertIn('Drupal 11.5.1 is out', msg)
        self.assertIn('MINOR=1 bfep update', msg)
        self.assertEqual(di.newer(str(p), minor=True,
                                  fetch=fake_fetch(['11.4.10' + V, '11.5.1' + V])), '')

    def test_prerelease_of_next_major_is_only_fyi(self):
        p = self.dockerfile(f'FROM drupal:11.4.10{V}')
        msg = di.newer(str(p), fetch=fake_fetch(['11.4.10' + V, '12.0.0-beta1' + V]))
        self.assertTrue(msg.startswith('FYI: Drupal 12.0.0-beta1'), msg)

    def test_new_major_lists_steps_and_module_readiness(self):
        custom = pathlib.Path(tempfile.mkdtemp(prefix='bfep-custom-'))
        (custom / 'bfep').mkdir()
        (custom / 'bfep' / 'bfep.info.yml').write_text("name: BFEP\ncore_version_requirement: '^10 || ^11'\n")
        (custom / 'butterfly').mkdir()
        (custom / 'butterfly' / 'butterfly.info.yml').write_text('core_version_requirement: ^11 || ^12\n')
        p = self.dockerfile(f'FROM drupal:11.4.10{V}')
        msg = di.newer(str(p), custom_dirs=[str(custom)], releases=self.releases,
                       fetch=fake_fetch(['11.4.10' + V, '12.0.0' + V, '12.0.2' + V, '12.0.2-php8.6-apache-trixie']))
        self.assertIn('NOTICE: Drupal 12 is out (newest: 12.0.2)', msg)
        self.assertRegex(msg, r'drupal/honeypot\s+ready: 2.3.0')
        self.assertRegex(msg, r'drupal/oidc\s+ready: 3.0.0-beta1 \(pre-release only\)')
        self.assertRegex(msg, r'bfep\s+needs updating')
        self.assertRegex(msg, r'butterfly\s+ready')
        self.assertIn(f'drupal:12.0.2{V}', msg)

    def test_unreachable_package_metadata_is_unknown(self):
        def boom(name):
            raise OSError('offline')
        p = self.dockerfile(f'FROM drupal:11.4.10{V}')
        msg = di.newer(str(p), releases=boom, fetch=fake_fetch(['11.4.10' + V, '12.0.0' + V]))
        self.assertIn('unknown (could not read packages.drupal.org: offline)', msg)


class SupportsTest(unittest.TestCase):

    def test_constraints(self):
        for c, want in (('^10.3 || ^11', False), ('^11 || ^12', True), ('^12.1', True),
                        ('>=10', True), ('>=10 <12', False), ('>=10 <13', True), ('~12.0', True),
                        ('12.x', True), ('^10|^12', True)):
            self.assertEqual(di.supports(c, 12), want, c)

    def test_expand_minified_metadata(self):
        v = di.expand([{'version': '2', 'require': {'a': '1'}, 'x': 1}, {'version': '1', 'x': '__unset'}])
        self.assertEqual(v[1], {'version': '1', 'require': {'a': '1'}})


if __name__ == '__main__':
    unittest.main()
