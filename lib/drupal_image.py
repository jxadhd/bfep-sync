"""Move a Dockerfile's official drupal base image to the newest patch release.

Used by `bfep update` before it rebuilds the image. The FROM line must name a
versioned tag, for example

    FROM drupal:11.4.8-php8.5-apache-trixie@sha256:...

and is rewritten to the newest tag with the same variant (-php8.5-apache-trixie)
in the same minor series (11.4.x), or with --minor in the same major (11.x),
pinned to that tag's digest from Docker Hub. A new major version is never
picked: that needs testing on a copy of the site first.

    python3 drupal_image.py dockerfile <compose-config.json> <service>
    python3 drupal_image.py bump <Dockerfile> [--minor] [--check]

`dockerfile` prints the path of the service's Dockerfile. `bump` prints one
line saying what changed and exits 0, or exits 3 if the FROM line can't be
handled (nothing is written then). With --check it only reports.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

HUB = 'https://hub.docker.com/v2/repositories/library/drupal/tags'
MAX_PAGES = 20

FROM_RE = re.compile(
    r'^(?P<lead>FROM\s+(?:--platform=\S+\s+)?)'
    r'(?P<repo>(?:docker\.io/)?(?:library/)?drupal):'
    r'(?P<version>\d+\.\d+\.\d+)(?P<variant>-[A-Za-z0-9.-]+)?'
    r'(?:@(?P<digest>sha256:[0-9a-f]{64}))?'
    r'(?P<rest>\s.*)?$')


class Unsupported(Exception):
    """The Dockerfile's base image is not one this can update."""


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split('.'))


def find_from(text: str) -> tuple[int, re.Match]:
    """The first FROM line, as (line index, match)."""
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if re.match(r'^FROM\s', line, re.I):
            m = FROM_RE.match(line.rstrip('\r'))
            if not m:
                raise Unsupported(
                    f'line {i + 1} is "{line.strip()}"; expected FROM drupal:<x.y.z>-<variant>[@sha256:...]')
            return i, m
    raise Unsupported('no FROM line')


def newest(tags: list[dict], current: str, variant: str, minor: bool) -> tuple[str, str] | None:
    """(version, digest) of the newest tag in the allowed series, or None."""
    major, mnr, _ = version_key(current)
    want = re.compile(r'^(\d+)\.(\d+)\.(\d+)' + re.escape(variant) + '$')
    best = None
    for t in tags:
        m = want.match(t.get('name', ''))
        digest = t.get('digest')
        if not m or not digest:
            continue
        v = tuple(int(x) for x in m.groups())
        if v[0] != major or (not minor and v[1] != mnr):
            continue
        if best is None or v > best[0]:
            best = (v, digest)
    if best is None:
        return None
    return '.'.join(map(str, best[0])), best[1]


def hub_tags(prefix: str) -> list[dict]:
    """All Docker Hub tags of the drupal image whose name contains prefix."""
    url = f'{HUB}?page_size=100&name={prefix}'
    out: list[dict] = []
    for _ in range(MAX_PAGES):
        req = urllib.request.Request(url, headers={'User-Agent': 'bfep-update'})
        with urllib.request.urlopen(req, timeout=30) as r:
            page = json.load(r)
        out += page.get('results', [])
        url = page.get('next')
        if not url:
            break
    return out


def bump(path: str, minor: bool = False, check: bool = False, tags: list[dict] | None = None) -> str:
    with open(path, encoding='utf-8') as f:
        text = f.read()
    i, m = find_from(text)
    current, variant = m['version'], m['variant'] or ''
    major, mnr, _ = version_key(current)
    if tags is None:
        tags = hub_tags(f'{major}.{mnr}.')
        # One query per later minor series until Docker Hub has none: a
        # single "11." query would page through thousands of old tags.
        nxt = mnr + 1
        while minor and nxt < mnr + 20:
            more = hub_tags(f'{major}.{nxt}.')
            if not more:
                break
            tags += more
            nxt += 1
    found = newest(tags, current, variant, minor)
    if found is None:
        raise Unsupported(f'Docker Hub lists no drupal:{major}.{mnr}.x{variant} tags')
    version, digest = found
    if version_key(version) < version_key(current):
        return f'drupal:{current}{variant} is newer than Docker Hub\'s newest ({version}); left as is'
    if version == current and digest == m['digest']:
        return f'drupal:{current}{variant} is already the newest image'
    line = f"{m['lead']}{m['repo']}:{version}{variant}@{digest}{m['rest'] or ''}"
    what = (f'drupal:{current}{variant} -> {version}{variant}' if version != current
            else f'drupal:{current}{variant} has a newer build')
    if check:
        return f'would move {what}'
    lines = text.split('\n')
    lines[i] = line
    tmp = f'{path}.bfep-new'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)
    return f'moved {what} ({digest[:19]})'


def dockerfile(config_json: str, service: str) -> str:
    """The Dockerfile path of a service in `docker compose config --format json`."""
    with open(config_json, encoding='utf-8') as f:
        cfg = json.load(f)
    build = cfg.get('services', {}).get(service, {}).get('build')
    if not build:
        raise Unsupported(f"service '{service}' has no build section")
    if isinstance(build, str):
        build = {'context': build}
    if build.get('dockerfile_inline'):
        raise Unsupported('the Dockerfile is inline in the compose file')
    return os.path.join(build.get('context', '.'), build.get('dockerfile') or 'Dockerfile')


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == 'dockerfile':
            print(dockerfile(argv[1], argv[2]))
        elif len(argv) >= 2 and argv[0] == 'bump' and set(argv[2:]) <= {'--minor', '--check'}:
            print(bump(argv[1], minor='--minor' in argv, check='--check' in argv))
        else:
            print(__doc__.strip().split('\n\n')[2], file=sys.stderr)
            return 2
    except (Unsupported, OSError, ValueError) as e:
        print(f'cannot update the base image: {e}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
