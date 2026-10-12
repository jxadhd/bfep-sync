"""python3 -m bfep_backup <command>: the R2 side of "bfep backup".

    upload FILE [--dry-run]   upload one backup, deleting old ones first as the limits require
    list                      list the bucket and its usage (one Class A request)
    usage                     this month's request counts (no request)
    fetch NAME DEST           download one backup (one Class B request)

Settings come from the environment (see deploy/bfep-backup.env.example).
Exit codes: 0 ok, 1 R2 or network error, 4 settings error, 6 refused to stay
inside the limits.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from .r2 import NAME, R2, BudgetError, Ledger, R2Error, gb, plan

FREE_STORAGE = 10e9      # R2 free tier: 10 GB-month of Standard storage
FREE_A, FREE_B = 1_000_000, 10_000_000


class ConfigError(Exception):
    pass


def number(env, name, default, low, high, kind=float):
    raw = env.get(name, '').strip() or str(default)
    try:
        v = kind(raw)
    except ValueError:
        raise ConfigError(f'{name}={raw!r} is not a number') from None
    if not low <= v <= high:
        raise ConfigError(f'{name}={raw} must be between {low} and {high}')
    return v


def settings(env):
    s = {}
    for name in ('R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'):
        s[name] = env.get(name, '').strip()
        if not s[name]:
            raise ConfigError(f'{name} is not set')
    endpoint = env.get('R2_ENDPOINT', '').strip()
    if not endpoint:
        account = env.get('R2_ACCOUNT_ID', '').strip()
        if not account:
            raise ConfigError('set R2_ACCOUNT_ID (or R2_ENDPOINT)')
        endpoint = f'https://{account}.r2.cloudflarestorage.com'
    s['endpoint'] = endpoint
    prefix = env.get('BACKUP_PREFIX', 'db/').strip()
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    s['prefix'] = prefix
    # Hard ceilings sit below the free tier whatever the settings file says,
    # so a typo cannot open the door to charges.
    s['max_bytes'] = int(number(env, 'BACKUP_MAX_GB', 8, 0.01, 9) * 1e9)
    s['max_object'] = int(number(env, 'BACKUP_MAX_OBJECT_GB', 2, 0.001, 4.5) * 1e9)
    s['keep_days'] = number(env, 'BACKUP_KEEP_DAYS', 30, 1, 3650, int)
    s['keep_min'] = number(env, 'BACKUP_KEEP_MIN', 7, 1, 1000, int)
    s['limits'] = {
        'A': number(env, 'BACKUP_MAX_CLASS_A', 1000, 1, FREE_A // 10, int),
        'B': number(env, 'BACKUP_MAX_CLASS_B', 1000, 1, FREE_B // 10, int),
        'free': number(env, 'BACKUP_MAX_DELETES', 1000, 1, 100_000, int),
    }
    s['ledger'] = env.get('BACKUP_LEDGER', '').strip() or '/var/lib/bfep-backup/r2-requests.json'
    return s


def client(s):
    return R2(s['endpoint'], s['R2_BUCKET'], s['R2_ACCESS_KEY_ID'], s['R2_SECRET_ACCESS_KEY'],
              Ledger(s['ledger'], s['limits']))


def show_usage(s, r2):
    c = r2.ledger.counts()
    lim = s['limits']
    print(f"Requests this month (UTC, by this tool): Class A {c['A']}/{lim['A']}, "
          f"Class B {c['B']}/{lim['B']}, deletes (free) {c['free']}/{lim['free']}")


def cmd_usage(s, args):
    show_usage(s, client(s))


def cmd_list(s, args):
    r2 = client(s)
    objects = r2.list()
    ours = sorted((o for o in objects if o['key'].startswith(s['prefix'])), key=lambda o: o['key'])
    for o in ours:
        print(f"{o['size']:>14,}  {o['modified']}  {o['key']}")
    total = sum(o['size'] for o in objects)
    other = len(objects) - len(ours)
    print(f"\n{len(ours)} backup(s) under {s['prefix'] or '/'}"
          + (f", plus {other} other object(s)" if other else ''))
    print(f"Bucket total {gb(total)} of the {gb(s['max_bytes'])} limit "
          f"(free tier {gb(FREE_STORAGE)}); keeping {s['keep_days']} days, at least {s['keep_min']}")
    show_usage(s, r2)


def cmd_fetch(s, args):
    if not NAME.match(args.name):
        raise ConfigError(f'{args.name!r} is not a backup name (see bfep backup list)')
    dest = os.path.join(args.dest, args.name) if os.path.isdir(args.dest) else args.dest
    if os.path.exists(dest):
        raise ConfigError(f'{dest} already exists')
    r2 = client(s)
    try:
        r2.get(s['prefix'] + args.name, dest + '.part')
    except BaseException:
        if os.path.exists(dest + '.part'):
            os.unlink(dest + '.part')
        raise
    os.replace(dest + '.part', dest)
    print(f'Saved {dest} ({gb(os.path.getsize(dest))})')
    show_usage(s, r2)


def cmd_upload(s, args):
    name = os.path.basename(args.file)
    if not NAME.match(name):
        raise ConfigError(f'{name!r} is not a backup name (bfep-db-YYYYmmddTHHMMSSZ.tar[.gpg])')
    size = os.path.getsize(args.file)
    r2 = client(s)
    objects = r2.list()
    p = plan(objects, s['prefix'], name, size, dt.datetime.now(dt.timezone.utc), s['max_bytes'],
             s['max_object'], s['keep_days'], s['keep_min'])
    print(f"Bucket holds {gb(p.total_before)} ({p.backups} backup(s)); limit {gb(s['max_bytes'])}")
    for key in p.expired:
        print(f'  delete {key} (older than {s["keep_days"]} days)')
    for key in p.make_room:
        print(f'  delete {key} (to make room)')
    if p.upload:
        print(f'  upload {s["prefix"]}{name} ({gb(size)}); bucket will hold {gb(p.total_after)}')
    if args.dry_run:
        print('Dry run: nothing deleted or uploaded')
    else:
        for key in p.deletes:
            r2.delete(key)
        if p.upload:
            r2.put(s['prefix'] + name, args.file)
            print(f'Uploaded {s["prefix"]}{name}')
    show_usage(s, r2)
    if p.refused:
        print(f'REFUSED: {p.refused}. Nothing was uploaded.', file=sys.stderr)
        return 6
    return 0


def main(argv=None, env=None):
    ap = argparse.ArgumentParser(prog='bfep backup', description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    u = sub.add_parser('upload')
    u.add_argument('file')
    u.add_argument('--dry-run', action='store_true')
    sub.add_parser('list')
    sub.add_parser('usage')
    f = sub.add_parser('fetch')
    f.add_argument('name')
    f.add_argument('dest')
    args = ap.parse_args(argv)
    try:
        s = settings(os.environ if env is None else env)
        return {'upload': cmd_upload, 'list': cmd_list, 'usage': cmd_usage,
                'fetch': cmd_fetch}[args.cmd](s, args) or 0
    except ConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 4
    except BudgetError as e:
        print(f'REFUSED: {e}', file=sys.stderr)
        return 6
    except (R2Error, OSError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
