"""A small S3 client for Cloudflare R2 that counts every request it makes.

Standard library only, so the server needs no AWS CLI, rclone or boto3. It
speaks the five calls the backup needs: ListObjectsV2, PutObject, GetObject,
HeadObject and DeleteObject. It never uses multipart uploads, so there are no
half-finished uploads that keep taking storage.

Every request goes through the Ledger first. The Ledger adds it to this
month's count on disk and refuses it if the count would pass the limit, so a
bug or a loop fails closed instead of running up Class A or Class B charges.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import re
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

# R2's own classes (https://developers.cloudflare.com/r2/pricing/).
OP_CLASS = {
    'ListObjectsV2': 'A',
    'PutObject': 'A',
    'GetObject': 'B',
    'HeadObject': 'B',
    'DeleteObject': 'free',
}
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
CHUNK = 1 << 20


class BudgetError(Exception):
    """A request was refused because it would pass a configured limit."""


class R2Error(Exception):
    def __init__(self, status, code, message):
        super().__init__(f'R2 returned {status} {code}: {message}')
        self.status, self.code = status, code


# Signing (AWS Signature Version 4, which R2 uses with region "auto")

def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def quote(s, safe='/'):
    return urllib.parse.quote(s, safe=safe + '-_.~')


def canonical_query(query):
    return '&'.join(f'{quote(k, "")}={quote(str(v), "")}' for k, v in sorted(query.items()))


def sign(method, host, path, query, headers, payload_hash, key_id, secret, when,
         region='auto', service='s3'):
    """Returns headers with x-amz-date, x-amz-content-sha256 and Authorization added.

    `path` is the unencoded object path ("/bucket/key"); `query` a dict.
    """
    amz_date = when.strftime('%Y%m%dT%H%M%SZ')
    day = amz_date[:8]
    out = dict(headers)
    out['host'] = host
    out['x-amz-date'] = amz_date
    out['x-amz-content-sha256'] = payload_hash
    canon_headers = {k.lower(): ' '.join(str(v).split()) for k, v in out.items()}
    signed = ';'.join(sorted(canon_headers))
    request = '\n'.join([
        method, quote(path), canonical_query(query),
        ''.join(f'{k}:{canon_headers[k]}\n' for k in sorted(canon_headers)),
        signed, payload_hash,
    ])
    scope = f'{day}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                         hashlib.sha256(request.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret).encode(), day), region), service), 'aws4_request')
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out['Authorization'] = (f'AWS4-HMAC-SHA256 Credential={key_id}/{scope}, '
                            f'SignedHeaders={signed}, Signature={signature}')
    return out


# Request ledger

class Ledger:
    """Counts requests per calendar month (UTC) in a JSON file and enforces limits.

    The count is written to disk before the request is sent, so a crash can
    only over-count. A missing file starts at zero; an unreadable or damaged
    one stops everything until someone looks at it.
    """

    def __init__(self, path, limits, clock=None):
        self.path = path
        self.limits = limits            # {'A': n, 'B': n, 'free': n}
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def month(self):
        return self.clock().strftime('%Y-%m')

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise BudgetError(f'cannot read the request ledger {self.path} ({e}); '
                              'fix or remove it by hand before running again') from e
        if not isinstance(data, dict):
            raise BudgetError(f'the request ledger {self.path} is damaged')
        return data

    def counts(self):
        c = self.load().get(self.month(), {})
        return {k: int(c.get(k, 0)) for k in self.limits}

    def spend(self, op):
        cls = OP_CLASS[op]
        data = self.load()
        month = self.month()
        c = data.setdefault(month, {})
        used = int(c.get(cls, 0))
        if used + 1 > self.limits[cls]:
            raise BudgetError(f'{op} refused: {used} Class {cls} requests already made in '
                              f'{month}, limit {self.limits[cls]}')
        c[cls] = used + 1
        for old in sorted(data)[:-13]:  # keep a year of history
            del data[old]
        self._write(data)

    def _write(self, data):
        d = os.path.dirname(self.path) or '.'
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.ledger-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise


# Client

class R2:
    def __init__(self, endpoint, bucket, key_id, secret, ledger, timeout=120, max_list_pages=20):
        u = urllib.parse.urlsplit(endpoint)
        if u.scheme not in ('https', 'http') or not u.hostname:
            raise ValueError(f'bad R2 endpoint {endpoint!r}')
        self.scheme, self.netloc = u.scheme, u.netloc
        self.bucket, self.key_id, self.secret = bucket, key_id, secret
        self.ledger, self.timeout, self.max_list_pages = ledger, timeout, max_list_pages
        self.requests = []  # (op, key) for each request sent, for the run summary

    def _conn(self):
        cls = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        return cls(self.netloc, timeout=self.timeout)

    def _send(self, op, method, key='', query=None, body=None, length=0, payload_hash=EMPTY_SHA256,
              headers=None, sink=None):
        query = query or {}
        path = f'/{self.bucket}' + (f'/{key}' if key else '')
        hdrs = sign(method, self.netloc, path, query, headers or {}, payload_hash,
                    self.key_id, self.secret, dt.datetime.now(dt.timezone.utc))
        if method in ('PUT', 'POST'):
            hdrs['Content-Length'] = str(length)
        url = quote(path) + ('?' + canonical_query(query) if query else '')
        self.ledger.spend(op)            # counted before it is sent
        self.requests.append((op, key))
        conn = self._conn()
        try:
            conn.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()
            if body is not None:
                while True:
                    chunk = body.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            if resp.status >= 300:
                data = resp.read()
                code, msg = 'Error', data[:200].decode('utf-8', 'replace')
                try:
                    root = ET.fromstring(data)
                    code = _find(root, 'Code') or code
                    msg = _find(root, 'Message') or msg
                except ET.ParseError:
                    pass
                raise R2Error(resp.status, code, msg)
            if sink is not None:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    sink.write(chunk)
                return resp, b''
            return resp, resp.read()
        finally:
            conn.close()

    def list(self, prefix=''):
        """All objects under prefix: [{'key', 'size', 'modified'}]. One Class A per 1000."""
        out, token = [], None
        for _ in range(self.max_list_pages):
            q = {'list-type': '2', 'max-keys': '1000'}
            if prefix:
                q['prefix'] = prefix
            if token:
                q['continuation-token'] = token
            _, data = self._send('ListObjectsV2', 'GET', query=q)
            root = ET.fromstring(data)
            for c in root:
                if _local(c.tag) == 'Contents':
                    out.append({'key': _find(c, 'Key'), 'size': int(_find(c, 'Size') or 0),
                                'modified': _find(c, 'LastModified')})
            if _find(root, 'IsTruncated') != 'true':
                return out
            token = _find(root, 'NextContinuationToken')
        raise BudgetError(f'the bucket holds more than {self.max_list_pages * 1000} objects; '
                          'refusing to keep listing it')

    def put(self, key, path):
        """Uploads one file in a single PutObject and checks R2 stored the same bytes."""
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(CHUNK), b''):
                sha.update(chunk)
                md5.update(chunk)
                size += len(chunk)
        with open(path, 'rb') as f:
            # STANDARD explicitly: the free tier does not cover Infrequent
            # Access, which also bills retrieval and a 30-day minimum.
            resp, _ = self._send('PutObject', 'PUT', key, body=f, length=size,
                                 payload_hash=sha.hexdigest(),
                                 headers={'x-amz-storage-class': 'STANDARD',
                                          'content-type': 'application/octet-stream'})
        etag = (resp.getheader('ETag') or '').strip('"')
        if etag and etag != md5.hexdigest():
            raise R2Error(resp.status, 'ChecksumMismatch',
                          f'R2 stored {key} with ETag {etag}, expected {md5.hexdigest()}')
        return size

    def get(self, key, dest):
        with open(dest, 'wb') as f:
            self._send('GetObject', 'GET', key, sink=f)

    def delete(self, key):
        self._send('DeleteObject', 'DELETE', key)


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _find(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c.text
    return None


# Choosing what to keep

NAME = re.compile(r'^bfep-db-(\d{8}T\d{6}Z)\.tar(\.gpg)?$')


def stamp(name):
    m = NAME.match(name)
    return dt.datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc) if m else None


class Plan:
    def __init__(self):
        self.expired, self.make_room, self.refused = [], [], None
        self.total_before = self.total_after = self.backups = 0

    @property
    def deletes(self):
        return self.expired + self.make_room

    @property
    def upload(self):
        return self.refused is None


def plan(objects, prefix, new_name, new_size, now, max_bytes, max_object, keep_days, keep_min):
    """Decides which old backups to delete and whether the new one may go up.

    Everything in the bucket counts towards max_bytes, but only our own
    backups under prefix are ever deleted. The keep_min newest backups already
    in the bucket are never deleted, even to make room: if the new one does
    not fit without that, it is refused instead. The bucket total never goes
    over max_bytes at any moment, because deletes run before the upload.
    """
    p = Plan()
    p.total_before = sum(o['size'] for o in objects)
    ours = []
    for o in objects:
        name = o['key'][len(prefix):] if o['key'].startswith(prefix) else None
        when = stamp(name) if name is not None and '/' not in name else None
        if when:
            ours.append((when, o))
    ours.sort(key=lambda t: t[0], reverse=True)
    p.backups = len(ours)
    protected, rest = ours[:keep_min], ours[keep_min:]
    cutoff = now - dt.timedelta(days=keep_days)
    p.expired = [o['key'] for when, o in rest if when < cutoff]
    total = p.total_before - sum(o['size'] for _, o in rest if o['key'] in p.expired)
    candidates = [o for _, o in reversed(rest) if o['key'] not in p.expired]  # oldest first

    if new_size > max_object:
        p.refused = f'{new_name} is {gb(new_size)}, over the {gb(max_object)} per-backup limit'
    elif any(o['key'] == prefix + new_name for o in objects):
        p.refused = f'{prefix}{new_name} is already in the bucket'
    else:
        while total + new_size > max_bytes and candidates:
            o = candidates.pop(0)
            p.make_room.append(o['key'])
            total -= o['size']
        if total + new_size > max_bytes:
            p.make_room = []
            total = p.total_before - sum(o['size'] for _, o in rest if o['key'] in p.expired)
            p.refused = (f'{new_name} ({gb(new_size)}) does not fit: the bucket would hold '
                         f'{gb(total + new_size)}, over the {gb(max_bytes)} limit, even after deleting '
                         f'every backup except the newest {keep_min}')
    p.total_after = total + (new_size if p.upload else 0)
    return p


def gb(n):
    return f'{n / 1e9:.3f} GB'
