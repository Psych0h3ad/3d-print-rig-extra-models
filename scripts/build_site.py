"""Build both independently checksum-pinned static bundles. Modified 2026-10-22."""
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_SITE_BYTES = 900_000_000
MAX_MANIFEST_BYTES = 8_000_000
RELEASE_PREFIX = 'https://github.com/Psych0h3ad/3d-print-rig-extra-models/releases/download/'
MACHINES = {
    'neptune3': ('Neptune 3.3DXML', '13df2d1e535c8e1bde689511d96693f2ae3229b6d295a7b27b18df0891cba58a'),
    'neptune3pro': ('Neptune 3 Pro.3DXML', '5293a146f9059b03a9e2d9837fc8bd75c534646752e90592bbd414e334e0f8c8'),
    'neptune3plus': ('Neptune 3 Plus.3DXML', 'a83902f5354349e717a153bfc05bc884bb84f1664608e655546c1c2d5b6a5b94'),
}
GPL_SHA256 = '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986'
RUNTIME_PATHS = {f'elegoo-assets/{slug}/{name}' for slug in MACHINES
                 for name in ('assembly_manifest.json', 'machine_profile.json', 'model.glb.gz')}
SOURCE_PATHS = {f'elegoo-sources/{name}' for name in (
    'LICENSE', 'README.md', 'NOTICE.txt', 'MODIFICATIONS.md', 'SOURCES.json',
    'models.json', 'requirements.txt', 'tools/extract_3dxml.py', 'tools/extract_3dxml_bounded.py', 'tools/rebuild_models.py')}
for _slug, (_filename, _) in MACHINES.items():
    SOURCE_PATHS.update((f'elegoo-sources/{_slug}/source/{_filename}',
                        f'elegoo-sources/{_slug}/LICENSE',
                        f'elegoo-sources/{_slug}/UPSTREAM_README.md'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def json_bytes(data):
    return json.loads(data, object_pairs_hook=unique_object)


def valid_count(value, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def load_pin(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Oversized bundle pin')
    info = json_bytes(raw)
    if (type(info.get('version')) is not int or info['version'] != 1
            or not all(valid_count(info.get(k), True) for k in ('bytes', 'files', 'site_bytes'))
            or info['site_bytes'] > MAX_SITE_BYTES or not valid_sha(info.get('sha256'))):
        raise ValueError('Invalid bundle pin')
    return info, raw


def asset_path(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('Invalid asset path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name:
        raise ValueError('Invalid asset path')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if any(part.endswith((' ', '.')) or any(ord(c) < 32 or c in '<>"|?*' for c in part)
           or part.split('.')[0].upper() in reserved for part in path.parts):
        raise ValueError('Invalid portable asset path')
    return path


def allowed_asset(name, kind):
    path = asset_path(name)
    if path.suffix.lower() in ('.3dxml', '.py'):
        return kind == 'elegoo' and name in SOURCE_PATHS
    return path.suffix in ('.json', '.gz', '.html', '.txt', '.md') or path.name == 'LICENSE' or name == '.nojekyll'


def check_elegoo(records, archive):
    indexed = {row['path']: row for row in records}
    if set(indexed) != RUNTIME_PATHS | SOURCE_PATHS:
        raise ValueError('ELEGOO runtime/source inventory incomplete or unexpected')
    for slug, (filename, source_sha) in MACHINES.items():
        if indexed[f'elegoo-sources/{slug}/source/{filename}']['sha256'] != source_sha:
            raise ValueError('ELEGOO original 3DXML source mismatch')
    for name in ('elegoo-sources/LICENSE', *(f'elegoo-sources/{slug}/LICENSE' for slug in MACHINES)):
        if indexed[name]['sha256'] != GPL_SHA256:
            raise ValueError('ELEGOO full unchanged license missing or changed')
    with zipfile.ZipFile(archive) as source:
        if source.getinfo('elegoo-sources/SOURCES.json').file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Oversized ELEGOO source declaration')
        provenance = json_bytes(source.read('elegoo-sources/SOURCES.json'))
    if provenance.get('license') != 'GPL-3.0' or provenance.get('source_scope') != 'source_3dxml_tessellation':
        raise ValueError('ELEGOO GPL source area not explicitly declared')


def inspect_archive(archive, info, kind):
    archive = Path(archive)
    if archive.stat().st_size != info['bytes'] or sha(archive) != info['sha256']:
        raise ValueError('Bundle checksum mismatch')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or 'VIEWER_ASSETS.json' not in names:
            raise ValueError('Bundle inventory mismatch')
        if source.getinfo('VIEWER_ASSETS.json').file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Oversized asset manifest')
        records = json_bytes(source.read('VIEWER_ASSETS.json'))['files']
        if not isinstance(records, list) or len(records) != info['files']:
            raise ValueError('Bundle inventory mismatch')
        indexed, total = {}, 0
        for row in records:
            name = row['path']
            if not allowed_asset(name, kind) or name == 'VIEWER_ASSETS.json':
                raise ValueError('Unsupported asset type or reserved path')
            if not valid_count(row.get('bytes')) or not valid_sha(row.get('sha256')):
                raise ValueError('Invalid asset size or checksum')
            if name in indexed:
                raise ValueError('Duplicate asset record')
            indexed[name] = row
            total += row['bytes']
            if total > MAX_SITE_BYTES:
                raise ValueError('Site exceeds size budget')
        if set(names) != set(indexed) | {'VIEWER_ASSETS.json'} or total != info['site_bytes']:
            raise ValueError('Bundle inventory or site size mismatch')
        for entry in entries:
            asset_path(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir() or mode not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                raise ValueError('Unsupported ZIP entry')
            if entry.filename != 'VIEWER_ASSETS.json' and entry.file_size != indexed[entry.filename]['bytes']:
                raise ValueError('Asset size mismatch')
    if kind == 'elegoo':
        check_elegoo(records, archive)
    return records


def check_collisions(record_sets, pin_names):
    names = [asset_path(name).as_posix().casefold() for name in pin_names]
    names += [row['path'].casefold() for records in record_sets for row in records]
    if len(names) != len(set(names)):
        raise ValueError('Asset collision')
    all_names = set(names)
    for name in names:
        if any(parent.as_posix() in all_names for parent in PurePosixPath(name).parents
               if parent.as_posix() != '.'):
            raise ValueError('Asset file/directory collision')


def extract_verified(archive, target, records):
    with zipfile.ZipFile(archive) as source:
        for row in records:
            destination = target.joinpath(*asset_path(row['path']).parts)
            if not destination.resolve().is_relative_to(target.resolve()) or destination.exists():
                raise ValueError('Asset collision')
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with source.open(row['path']) as stream, destination.open('xb') as output:
                for data in iter(lambda: stream.read(1024 * 1024), b''):
                    size += len(data)
                    if size > row['bytes']:
                        raise ValueError('Unexpected expanded asset size')
                    digest.update(data)
                    output.write(data)
            if size != row['bytes'] or digest.hexdigest() != row['sha256']:
                raise ValueError('Asset checksum mismatch')


def acquire(info, offline, folder, name):
    if offline is not None:
        return Path(offline)
    url = info.get('url')
    if not isinstance(url, str) or not url.startswith(RELEASE_PREFIX):
        raise ValueError('Missing or unexpected release URL; no fallback')
    parsed = urllib.parse.urlsplit(url)
    if parsed.query or parsed.fragment or '..' in parsed.path.split('/'):
        raise ValueError('Unexpected release URL')
    archive = folder / name
    request = urllib.request.Request(url, headers={'User-Agent': '3D-Print-Rig'})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as stream, archive.open('xb') as output:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(data)
            if total > info['bytes']:
                raise ValueError('Unexpected download size')
            output.write(data)
    return archive


def build(target, assets=None, elegoo_assets=None, *, manifest_dir=None):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError('Use a new output directory')
    manifest_dir = Path(manifest_dir) if manifest_dir else ROOT / 'site'
    specs = [('ASSET_BUNDLE.json', 'legacy', assets), ('ELEGOO_ASSET_BUNDLE.json', 'elegoo', elegoo_assets)]
    pins = [(name, kind, offline, *load_pin(manifest_dir / name)) for name, kind, offline in specs]
    if sum(info['site_bytes'] + len(raw) for _, _, _, info, raw in pins) > MAX_SITE_BYTES:
        raise ValueError('Combined site exceeds size budget')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='assets-build-', dir=target.parent) as temporary:
        folder, bundles = Path(temporary), []
        for index, (name, kind, offline, info, raw) in enumerate(pins):
            archive = acquire(info, offline, folder, f'bundle-{index}.zip')
            records = inspect_archive(archive, info, kind)
            bundles.append((archive, records, name, raw))
        check_collisions([records for _, records, _, _ in bundles], [name for name, *_ in pins])
        stage = folder / 'site'
        stage.mkdir()
        for archive, records, name, raw in bundles:
            extract_verified(archive, stage, records)
            (stage / name).write_bytes(raw)
        total = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
        if total > MAX_SITE_BYTES:
            raise ValueError('Site exceeds size budget')
        os.rename(stage, target)
    result = {'files': sum(len(records) for _, records, _, _ in bundles), 'site_bytes': total, 'bundles': 2}
    print(f"Built {result['files']} verified asset files from both bundles ({total:,} bytes).")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assets', type=Path, help='Exact offline original ZIP')
    parser.add_argument('--elegoo-assets', type=Path, help='Exact offline second ELEGOO ZIP')
    args = parser.parse_args()
    build(args.output, args.assets, args.elegoo_assets)


if __name__ == '__main__':
    main()
