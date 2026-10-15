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

    def test_digest_only_or_other_images_are_refused(self):
        for line in (f"FROM drupal@{D['11.4.8']}", 'FROM drupal:11.4-php8.5-apache-trixie',
                     'FROM php:8.5-apache'):
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


if __name__ == '__main__':
    unittest.main()
