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
    python3 drupal_image.py newer <Dockerfile> [--minor] [<custom dir>...]

`dockerfile` prints the path of the service's Dockerfile. `bump` prints one
line saying what changed and exits 0, or exits 3 if the FROM line can't be
handled (nothing is written then). With --check it only reports.

`newer` prints a notice when a release that `bump` won't move to is out: a
new minor (without --minor) or a new major version, with the steps to take
and, for a major, whether each contrib module in the Dockerfile and each
custom module or theme under the given folders already supports it. It
prints nothing when there is nothing newer, and always exits 0.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.request

HUB = 'https://hub.docker.com/v2/repositories/library/drupal/tags'
DRUPAL_PACKAGES = 'https://packages.drupal.org/files/packages/8/p2/{name}.json'
RELEASE_NOTES = 'https://www.drupal.org/project/drupal/releases/{version}'
UPGRADE_GUIDE = 'https://www.drupal.org/docs/upgrading-drupal'
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


STABLE_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)(-.*)?$')
PRERELEASE_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)-(alpha|beta|rc)\d*(-.*)?$')


def series(major: int, start: int, fetch=None) -> list[dict]:
    """Tags of major.start.x, major.start+1.x, ... until a series has none."""
    fetch = fetch or hub_tags
    out: list[dict] = []
    for m in range(start, start + 20):
        more = fetch(f'{major}.{m}.')
        if not more:
            break
        out += more
    return out


def newest_stable(tags: list[dict], major: int, min_minor: int = 0) -> tuple[str, set[str]] | None:
    """(version, its variants) of the newest stable release in a major."""
    found: dict[tuple[int, ...], set[str]] = {}
    for t in tags:
        name = t.get('name', '')
        m = STABLE_RE.match(name)
        if not m or PRERELEASE_RE.match(name):
            continue
        v = tuple(int(x) for x in m.groups()[:3])
        if v[0] == major and v[1] >= min_minor:
            found.setdefault(v, set()).add(m[4] or '')
    if not found:
        return None
    v = max(found)
    return '.'.join(map(str, v)), found[v]


def newest_prerelease(tags: list[dict], major: int) -> str | None:
    best = None
    for t in tags:
        m = PRERELEASE_RE.match(t.get('name', ''))
        if m and int(m[1]) == major:
            label = t['name'][:len(t['name']) - len(m[5] or '')]
            if best is None or label > best:
                best = label
    return best


def supports(constraint: str, major: int) -> bool:
    """Whether a Composer constraint such as "^10.3 || ^11" allows the major."""
    for alt in re.split(r'\s*\|\|?\s*', constraint.strip()):
        parts = [x for x in re.split(r'[\s,]+', alt) if x]
        lows, highs, ok = [], [], False
        for x in parts:
            m = re.match(r'^(\^|~|>=|>|<=|<|==|=)?v?(\d+)', x)
            if not m:
                continue
            op, n = m[1] or '', int(m[2])
            if op in ('^', '~', '', '=', '=='):
                ok = ok or n == major
            elif op in ('>=', '>'):
                lows.append(n)
            else:
                highs.append((op, n))
        if ok:
            return True
        if lows and all(n <= major for n in lows) and all(
                n > major or (op == '<=' and n == major) for op, n in highs):
            return True
    return False


def expand(versions: list[dict]) -> list[dict]:
    """Undo Composer 2 metadata minification (each entry lists only changes)."""
    out, prev = [], {}
    for v in versions:
        cur = dict(prev)
        for k, val in v.items():
            if val == '__unset':
                cur.pop(k, None)
            else:
                cur[k] = val
        out.append(cur)
        prev = cur
    return out


def drupal_releases(name: str) -> list[dict]:
    url = DRUPAL_PACKAGES.format(name=name)
    req = urllib.request.Request(url, headers={'User-Agent': 'bfep-update'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return expand(data.get('packages', {}).get(name, []))


def contrib_status(name: str, major: int, releases) -> str:
    try:
        versions = releases(name)
    except (OSError, ValueError) as e:
        return f'unknown (could not read packages.drupal.org: {e})'
    stable = [v for v in versions if not re.search(r'(alpha|beta|rc|dev)', v.get('version', ''), re.I)]
    for group, label in ((stable, ''), (versions, ' (pre-release only)')):
        for v in group:
            core = v.get('require', {}).get('drupal/core', '')
            if core and supports(core, major):
                return f'ready: {v["version"]}{label}'
    return 'NOT ready yet'


def contrib_modules(text: str) -> list[str]:
    names = re.findall(r'["\s](drupal/[a-z0-9_]+):', text)
    return sorted({n for n in names if not n.startswith('drupal/core')})


def custom_status(dirs: list[str], major: int) -> list[str]:
    lines = []
    for d in dirs:
        for info in sorted(pathlib.Path(d).glob('*/*.info.yml')):
            m = re.search(r'^core_version_requirement:\s*[\'"]?([^\'"\n]+)', info.read_text(), re.M)
            if not m:
                state = 'no core_version_requirement'
            else:
                state = ('ready' if supports(m[1], major) else 'needs updating') + f' ({m[1].strip()})'
            lines.append(f'    {info.parent.name:<22} {state}')
    return lines


def newer(path: str, minor: bool = False, custom_dirs: list[str] | None = None,
          fetch=None, releases=None) -> str:
    """A notice about releases `bump` won't move to, or '' if there are none."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    _, m = find_from(text)
    current, variant = m['version'], m['variant'] or ''
    major, mnr, _ = version_key(current)
    releases = releases or drupal_releases
    out: list[str] = []

    if not minor:
        found = newest_stable(series(major, mnr + 1, fetch), major, mnr + 1)
        if found:
            v = found[0]
            out += [
                f'NOTICE: Drupal {v} is out; this update stays on {major}.{mnr}.x.',
                f'  Release notes: {RELEASE_NOTES.format(version=v)}',
                '  Minor releases add features and deprecations but are meant to be backward compatible;',
                '  database updates run automatically. When ready:  MINOR=1 bfep update',
                '']

    nxt = major + 1
    tags = series(nxt, 0, fetch)
    found = newest_stable(tags, nxt)
    if not found:
        pre = newest_prerelease(tags, nxt)
        if pre:
            out += [f'FYI: Drupal {pre} (a pre-release of Drupal {nxt}) is out. Nothing to do until {nxt}.0.0 is stable.', '']
        return '\n'.join(out).rstrip()

    v, variants = found
    if variant in variants:
        tag = f'{v}{variant}'
    else:
        tag = f'{v}<variant>  (no {variant.lstrip("-")} build; available: ' + \
              ', '.join(sorted(x.lstrip('-') for x in variants if x)) + ')'
    out += [
        f'NOTICE: Drupal {nxt} is out (newest: {v}). You are on {current}.',
        '  bfep update never moves to a new major version by itself.',
        f'  Release notes: {RELEASE_NOTES.format(version=v)}',
        f'  Upgrade guide: {UPGRADE_GUIDE}',
        f'  Contrib modules (newest release that allows Drupal {nxt}):']
    for name in contrib_modules(text):
        out.append(f'    {name:<22} {contrib_status(name, nxt, releases)}')
    out.append('    drush/drush            check https://www.drush.org for the version that supports it')
    custom = custom_status(custom_dirs or [], nxt)
    if custom:
        out += [f'  Custom modules and themes (core_version_requirement must allow ^{nxt}):'] + custom
    out += [
        '  Steps:',
        '    1. Wait until every module above is ready (or replace the ones that are not).',
        '    2. Run the Upgrade Status module (drupal/upgrade_status) on a copy of the site',
        '       and fix what it reports in the custom modules and theme.',
        f'    3. In the site repo, change the FROM line of Dockerfile.drupal to drupal:{tag},',
        f'       raise the contrib constraints that need it, set EXPECTED_DRUPAL_MAJOR to {nxt}',
        '       in .github/workflows/image.yml, and let the image build check pass.',
        '    4. Make the same FROM change in the server Dockerfile and run bfep update.',
        '       It backs up first; roll back with: bfep update rollback <timestamp>']
    return '\n'.join(out).rstrip()


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
        elif len(argv) >= 2 and argv[0] == 'newer':
            try:
                msg = newer(argv[1], minor='--minor' in argv[2:],
                            custom_dirs=[d for d in argv[2:] if d != '--minor'])
            except (Unsupported, OSError, ValueError) as e:
                msg = f'(could not check for newer Drupal releases: {e})'
            if msg:
                print(msg)
        else:
            print(__doc__.strip().split('\n\n')[2], file=sys.stderr)
            return 2
    except (Unsupported, OSError, ValueError) as e:
        print(f'cannot update the base image: {e}', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
