"""Publish exact, versioned viewer chunks without expanding compressed geometry."""
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import urllib.request
import zipfile

PIN = 'SOURCE_CHUNKS.json'
RELEASE = 'https://github.com/Psych0h3ad/3d-print-rig-extra-models/releases/download/'
MAX_TRANSFER = 128 * 1024 * 1024
MAX_RAW = 512 * 1024 * 1024
MAX_CHUNK = 16 * 1024 * 1024
MAX_NOTICE = 1024 * 1024
MAX_JSON = 8 * 1024 * 1024
INVENTORY = 'inventory-rh3d_e3ng_v12_235.json'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def object_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate chunk metadata key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('Non-finite JSON number: ' + value)


def hash_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def integer(value, limit):
    return type(value) is int and 0 < value <= limit


def load_plan(directory):
    path = Path(directory) / PIN
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('Chunk pin must be a regular file')
    if not path.exists():
        return None
    with path.open('rb') as stream:
        raw = stream.read(MAX_NOTICE + 1)
    if len(raw) > MAX_NOTICE:
        raise ValueError('Oversized chunk pin')
    info = json.loads(raw, object_pairs_hook=object_unique, parse_constant=reject_constant)
    if (not isinstance(info, dict) or set(info) != {'version', 'model_sha256', 'prefix', 'url', 'bytes', 'sha256', 'files'}
            or type(info['version']) is not int or info['version'] != 1
            or not hash_value(info['model_sha256']) or not hash_value(info['sha256'])
            or not integer(info['bytes'], MAX_TRANSFER)
            or info['prefix'] != 'source-assemblies/' + info['model_sha256']):
        raise ValueError('Invalid chunk pin')
    if (not isinstance(info['url'], str) or not info['url'].startswith(RELEASE)
            or re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*\.zip', info['url'][len(RELEASE):]) is None):
        raise ValueError('Unexpected chunk archive URL')
    records = info['files']
    if not isinstance(records, list) or not 3 <= len(records) <= 130:
        raise ValueError('Invalid chunk inventory')
    names = set()
    json_names = {'SOURCES.json', INVENTORY, 'manifest-' + info['model_sha256'] + '.json'}
    decoded_bytes = 0
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise ValueError('Invalid chunk record')
        name = row['name']
        compressed = name.endswith('.bin.gz')
        binary = name.endswith('.bin')
        limit = MAX_CHUNK if compressed or binary else MAX_JSON if name in json_names else MAX_NOTICE
        fields = {'name', 'bytes', 'sha256'} | ({'raw_bytes', 'raw_sha256'} if compressed else set())
        if (set(row) != fields or name.lower() in names or not hash_value(row['sha256'])
                or not integer(row['bytes'], limit)):
            raise ValueError('Invalid or repeated chunk record')
        names.add(name.lower())
        if compressed:
            if (not hash_value(row['raw_sha256']) or name != row['raw_sha256'] + '.bin.gz'
                    or not integer(row['raw_bytes'], MAX_CHUNK)):
                raise ValueError('Invalid decoded chunk identity')
            decoded_bytes += row['raw_bytes']
        elif binary:
            if name != row['sha256'] + '.bin':
                raise ValueError('Invalid raw chunk identity')
            decoded_bytes += row['bytes']
        elif name != 'NOTICE.txt' and name not in json_names:
            raise ValueError('Unexpected non-geometry file')
    if (not {'notice.txt', 'sources.json'} <= names
            or not any(name.endswith(('.bin', '.bin.gz')) for name in names)
            or sum(r['bytes'] for r in records) > MAX_TRANSFER
            or decoded_bytes > MAX_RAW):
        raise ValueError('Incomplete notices or oversized chunk set')
    return info, raw


def check_public_text(text):
    # Match local paths without treating ordinary public URL slashes as paths.
    if re.search(r'(?i)(?<![A-Z0-9])[A-Z]:[\\/]|\\\\|(?<!:)//[\w.-]+[\\/]'
                 r'|\bfile:[\\/]|(?<![\w])(?:~[\\/]|/(?:Users|home|private|mnt|tmp|var|Volumes)/)'
                 r'|%USERPROFILE%|\$HOME[\\/]|\.private\b', text):
        raise ValueError('Non-public path in chunk notice')


def check_public_json(text):
    pending = [json.loads(text, object_pairs_hook=object_unique, parse_constant=reject_constant)]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            check_public_text(value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Non-finite JSON number')


def check_payload(path, row):
    if path.stat().st_size != row['bytes'] or digest(path) != row['sha256']:
        raise ValueError('Chunk identity differs')
    if row['name'].endswith('.bin.gz'):
        total, checksum = 0, hashlib.sha256()
        with gzip.open(path, 'rb') as stream:
            while data := stream.read(min(1024 * 1024, row['raw_bytes'] - total + 1)):
                total += len(data)
                if total > row['raw_bytes']:
                    raise ValueError('Decoded chunk exceeds pinned size')
                checksum.update(data)
        if total != row['raw_bytes'] or checksum.hexdigest() != row['raw_sha256']:
            raise ValueError('Decoded chunk identity differs')
    elif not row['name'].endswith('.bin'):
        # Raw binary identity was checked above; only textual payloads need decoding.
        text = path.read_text('utf-8')
        if row['name'].endswith('.json'):
            check_public_json(text)
        else:
            check_public_text(text)


def install(plan, folder, target, offline=None):
    if plan is None:
        return 0
    info, raw = plan
    archive = Path(offline) if offline else folder / 'source-chunks.zip'
    if not offline:
        request = urllib.request.Request(info['url'], headers={'User-Agent': '3D-Print-Rig'})
        total = 0
        with urllib.request.urlopen(request, timeout=60) as stream, archive.open('xb') as output:
            while data := stream.read(1024 * 1024):
                total += len(data)
                if total > info['bytes']:
                    raise ValueError('Chunk archive exceeds pinned size')
                output.write(data)
    if archive.stat().st_size != info['bytes'] or digest(archive) != info['sha256']:
        raise ValueError('Chunk archive identity differs')
    expected = {r['name']: r for r in info['files']}
    destination = target / info['prefix']
    if destination.exists() or (target / PIN).exists():
        raise ValueError('Chunk output collision')
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        if len(members) != len(expected) or {m.filename for m in members} != set(expected):
            raise ValueError('Chunk archive inventory differs')
        for member in members:
            row = expected[member.filename]
            mode = member.external_attr >> 16
            if (member.file_size != row['bytes'] or member.is_dir() or member.flag_bits & 1
                    or stat.S_ISLNK(mode) or member.compress_type != zipfile.ZIP_STORED):
                raise ValueError('Invalid stored chunk member')
        destination.mkdir(parents=True)
        for member in members:
            row = expected[member.filename]
            path, total = destination / member.filename, 0
            with zipped.open(member) as source, path.open('xb') as output:
                while data := source.read(min(1024 * 1024, row['bytes'] - total + 1)):
                    total += len(data)
                    if total > row['bytes']:
                        raise ValueError('Chunk member exceeds pinned size')
                    output.write(data)
            check_payload(path, row)
    (target / PIN).write_bytes(raw)
    return len(expected) + 1
