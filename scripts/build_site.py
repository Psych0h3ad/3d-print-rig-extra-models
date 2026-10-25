"""Build independently checksum-pinned static asset bundles."""
import argparse
import gzip
import hashlib
import json
import math
import os
import re
import stat
import struct
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_SITE_BYTES = 900_000_000
MAX_MANIFEST_BYTES = 8_000_000
LAYOUT_PIN = 'LAYOUT_ASSET_BUNDLE.json'
REFERENCE_SET = 'LAYOUT_REFERENCE_SET.json'
REFERENCE_NAMES = (
    'U1_exterior_standalone_loaded.glb.gz', 'U1_exterior_docked.glb.gz',
    'PopStationMini_empty.glb.gz', 'PopStationMini_loaded.glb.gz',
    'U1_preview_geometry.json.gz', 'U1_exterior_notices.json', 'MOUNT_SPEC.json',
    'POPSTATION_PUBLIC_MOUNT_RECEIPTS.json', 'POPSTATION_PUBLIC_MANUAL_RECEIPTS.json',
    'U1_BED_SOURCE_RECEIPT.json', 'Snapmaker_OrcaSlicer_LICENSE.txt')
LAYOUT_DIRECTORY = 'layout/assets/equipment/u1-exterior'
LAYOUT_MODELS = frozenset(f'{LAYOUT_DIRECTORY}/{name}.glb.gz' for name in (
    'U1_exterior_standalone_loaded', 'U1_exterior_docked',
    'PopStationMini_empty', 'PopStationMini_loaded'))
LAYOUT_NOTICE = f'{LAYOUT_DIRECTORY}/NOTICE.txt'
LAYOUT_PREVIEW = f'{LAYOUT_DIRECTORY}/U1_preview_geometry.json.gz'
LAYOUT_PATHS = LAYOUT_MODELS | {LAYOUT_NOTICE, LAYOUT_PREVIEW}
MAX_LAYOUT_BYTES = 64_000_000
MAX_LAYOUT_ARCHIVE_BYTES = 65_000_000
MAX_LAYOUT_MODEL_BYTES = 16 * 1024 * 1024
MAX_LAYOUT_GLB_BYTES = 16 * 1024 * 1024
MAX_LAYOUT_MANIFEST_BYTES = 65_536
MAX_LAYOUT_NOTICE_BYTES = 4_096
MAX_LAYOUT_PREVIEW_BYTES = 16 * 1024 * 1024
RELEASE_PREFIX = 'https://github.com/Psych0h3ad/3d-print-rig-extra-models/releases/download/'
MACHINES = {
    'neptune3': ('Neptune 3.3DXML', '13df2d1e535c8e1bde689511d96693f2ae3229b6d295a7b27b18df0891cba58a'),
    'neptune3pro': ('Neptune 3 Pro.3DXML', '5293a146f9059b03a9e2d9837fc8bd75c534646752e90592bbd414e334e0f8c8'),
    'neptune3plus': ('Neptune 3 Plus.3DXML', 'a83902f5354349e717a153bfc05bc884bb84f1664608e655546c1c2d5b6a5b94'),
}
GPL_SHA256 = '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986'
RUNTIME_PATHS = {f'elegoo-assets/{slug}/{name}' for slug in MACHINES
                 for name in ('assembly_manifest.json', 'machine_profile.json', 'model.glb.gz')}
RUNTIME_PATHS.update(f'elegoo-assets/{slug}/zero-facets-v1/{name}'
                     for slug in ('neptune3pro','neptune3plus')
                     for name in ('assembly_manifest.json','machine_profile.json','model.glb.gz'))
SOURCE_PATHS = {f'elegoo-sources/{name}' for name in (
    'LICENSE', 'README.md', 'NOTICE.txt', 'MODIFICATIONS.md', 'SOURCES.json',
    'models.json', 'requirements.txt', 'tools/extract_3dxml.py', 'tools/extract_3dxml_bounded.py', 'tools/rebuild_models.py',
    'tools/filter_elegoo_zero_facets.py', 'ZERO_FACETS.md')}
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


def finite_json_bytes(data):
    def invalid_constant(value):
        raise ValueError('Non-finite JSON number')
    document = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Non-finite JSON number')
    return document


def canonical_json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode('ascii')


def read_bounded(path, limit):
    path = Path(path)
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Unsupported linked file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Oversized bundle pin')
    return data


def valid_count(value, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def load_pin(path):
    raw = read_bounded(path, MAX_MANIFEST_BYTES)
    info = json_bytes(raw)
    if (not isinstance(info, dict) or type(info.get('version')) is not int or info['version'] != 1
            or not all(valid_count(info.get(k), True) for k in ('bytes', 'files', 'site_bytes'))
            or max(info['bytes'], info['site_bytes']) > MAX_SITE_BYTES
            or info['files'] > 10_000 or not valid_sha(info.get('sha256'))):
        raise ValueError('Invalid bundle pin')
    return info, raw


def check_layout_pin(info):
    fields = {'version', 'url', 'bytes', 'sha256', 'files', 'site_bytes',
              'manifest_bytes', 'manifest_sha256'}
    if (set(info) != fields or info['files'] != len(LAYOUT_PATHS)
            or info['bytes'] > MAX_LAYOUT_ARCHIVE_BYTES or info['site_bytes'] > MAX_LAYOUT_BYTES
            or not valid_count(info['manifest_bytes'], True)
            or info['manifest_bytes'] > MAX_LAYOUT_MANIFEST_BYTES
            or not valid_sha(info['manifest_sha256'])):
        raise ValueError('Invalid layout bundle pin')
    if info['url'] is not None:
        check_release_url(info['url'])


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
    if kind == 'layout':
        return name in LAYOUT_PATHS
    if path.suffix.lower() in ('.3dxml', '.py'):
        return kind == 'elegoo' and name in SOURCE_PATHS
    return path.suffix in ('.json', '.gz', '.html', '.txt', '.md') or path.name == 'LICENSE' or name == '.nojekyll'


def check_layout_text(text, *, multiline=False):
    if any(not c.isprintable() and not (multiline and c == '\n') for c in text):
        raise ValueError('Invalid layout text control character')
    # Match concrete local paths without treating ordinary URL slashes as paths.
    if re.search(r'(?i)\b[a-z]:[\\/]|\\\\|(?<!:)//[\w.-]+[\\/]|\bfile:[\\/]'
                 r'|(?<![\w])(?:~[\\/]|/(?:Users|home|private|mnt|tmp|var|Volumes)/)'
                 r'|%USERPROFILE%|\$HOME[\\/]', text):
        raise ValueError('Invalid layout text local path')


def check_layout_notice(data):
    if not 0 < len(data) <= MAX_LAYOUT_NOTICE_BYTES:
        raise ValueError('Invalid layout NOTICE size')
    text = data.decode('utf-8')
    if not text.strip():
        raise ValueError('Invalid layout NOTICE text')
    check_layout_text(text, multiline=True)


def check_layout_model(path):
    """Bound gzip expansion and check the GLB container without changing its bytes."""
    if not 0 < Path(path).stat().st_size <= MAX_LAYOUT_MODEL_BYTES:
        raise ValueError('Invalid encoded layout GLB size')
    with gzip.open(path, 'rb') as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError('Invalid layout GLB header')
        magic, version, length = struct.unpack('<4sII', header)
        if magic != b'glTF' or version != 2 or not 20 <= length <= MAX_LAYOUT_GLB_BYTES:
            raise ValueError('Invalid layout GLB header or size')
        total = len(header)
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(data)
            if total > length:
                raise ValueError('Unexpected expanded layout GLB size')
        if total != length:
            raise ValueError('Layout GLB size mismatch')


def check_layout_preview(path):
    """Read a bounded JSON dictionary as data, without interpreting its strings."""
    if not 0 < Path(path).stat().st_size <= MAX_LAYOUT_PREVIEW_BYTES:
        raise ValueError('Invalid encoded layout preview size')
    with gzip.open(path, 'rb') as stream:
        raw = stream.read(MAX_LAYOUT_PREVIEW_BYTES + 1)
    if not 0 < len(raw) <= MAX_LAYOUT_PREVIEW_BYTES:
        raise ValueError('Invalid expanded layout preview size')
    document = finite_json_bytes(raw.decode('utf-8'))
    if not isinstance(document, dict):
        raise ValueError('Layout preview must be a JSON dictionary')
    return document


def layout_asset_limit(name):
    if name == LAYOUT_NOTICE:
        return MAX_LAYOUT_NOTICE_BYTES
    if name == LAYOUT_PREVIEW:
        return MAX_LAYOUT_PREVIEW_BYTES
    return MAX_LAYOUT_MODEL_BYTES


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
    if archive.is_symlink():
        raise ValueError('Unsupported bundle file')
    archive_stat = archive.stat()
    if not stat.S_ISREG(archive_stat.st_mode):
        raise ValueError('Unsupported bundle file')
    if kind == 'layout':
        check_layout_pin(info)
    if archive_stat.st_size != info['bytes'] or sha(archive) != info['sha256']:
        raise ValueError('Bundle checksum mismatch')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or 'VIEWER_ASSETS.json' not in names:
            raise ValueError('Bundle inventory mismatch')
        if kind == 'layout' and (len(entries) != len(LAYOUT_PATHS) + 1 or source.comment):
            raise ValueError('Layout bundle inventory or ZIP comment mismatch')
        for entry in entries:
            asset_path(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.orig_filename != entry.filename or entry.is_dir()
                    or mode not in (0, stat.S_IFREG) or entry.flag_bits & 1
                    or entry.external_attr & 0x10):
                raise ValueError('Unsupported ZIP entry')
            if kind == 'layout' and (entry.compress_type != zipfile.ZIP_STORED
                                      or entry.comment or entry.extra):
                raise ValueError('Unsupported layout ZIP entry')
        manifest_entry = source.getinfo('VIEWER_ASSETS.json')
        if manifest_entry.file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Oversized asset manifest')
        if kind == 'layout' and manifest_entry.file_size != info['manifest_bytes']:
            raise ValueError('Layout manifest size mismatch')
        raw_manifest = source.read('VIEWER_ASSETS.json')
        if kind == 'layout' and hashlib.sha256(raw_manifest).hexdigest() != info['manifest_sha256']:
            raise ValueError('Layout manifest checksum mismatch')
        manifest = json_bytes(raw_manifest)
        if not isinstance(manifest, dict):
            raise ValueError('Invalid asset manifest')
        if kind == 'layout' and (set(manifest) != {'version', 'files'}
                                 or type(manifest.get('version')) is not int or manifest['version'] != 1
                                 or raw_manifest != canonical_json(manifest)):
            raise ValueError('Invalid canonical layout manifest')
        records = manifest.get('files')
        if not isinstance(records, list) or len(records) != info['files']:
            raise ValueError('Bundle inventory mismatch')
        indexed, total = {}, 0
        for row in records:
            if not isinstance(row, dict) or 'path' not in row:
                raise ValueError('Invalid asset record')
            if kind == 'layout' and set(row) != {'path', 'bytes', 'sha256'}:
                raise ValueError('Invalid layout asset record')
            name = row['path']
            if not allowed_asset(name, kind) or name == 'VIEWER_ASSETS.json':
                raise ValueError('Unsupported asset type or reserved path')
            if not valid_count(row.get('bytes')) or not valid_sha(row.get('sha256')):
                raise ValueError('Invalid asset size or checksum')
            if name in indexed:
                raise ValueError('Duplicate asset record')
            if kind == 'layout' and not 0 < row['bytes'] <= layout_asset_limit(name):
                raise ValueError('Invalid layout asset size')
            indexed[name] = row
            total += row['bytes']
            if total > MAX_SITE_BYTES:
                raise ValueError('Site exceeds size budget')
        if set(names) != set(indexed) | {'VIEWER_ASSETS.json'} or total != info['site_bytes']:
            raise ValueError('Bundle inventory or site size mismatch')
        for entry in entries:
            if entry.filename != 'VIEWER_ASSETS.json' and entry.file_size != indexed[entry.filename]['bytes']:
                raise ValueError('Asset size mismatch')
        check_collisions([records], ['VIEWER_ASSETS.json'])
        if kind == 'layout':
            if set(indexed) != LAYOUT_PATHS:
                raise ValueError('Layout inventory incomplete or unexpected')
            if [row['path'] for row in records] != sorted(LAYOUT_PATHS):
                raise ValueError('Layout records must be sorted')
            check_layout_notice(source.read(LAYOUT_NOTICE))
    if kind == 'elegoo':
        check_elegoo(records, archive)
    return records


def check_collisions(record_sets, pin_names):
    names = [asset_path(name).as_posix().casefold() for name in pin_names]
    names += [asset_path(row['path']).as_posix().casefold() for records in record_sets for row in records]
    if len(names) != len(set(names)):
        raise ValueError('Asset collision')
    all_names = set(names)
    for name in names:
        if any(parent.as_posix() in all_names for parent in PurePosixPath(name).parents
               if parent.as_posix() != '.'):
            raise ValueError('Asset file/directory collision')


def extract_verified(archive, target, records, *, kind=None):
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
            if kind == 'layout' and row['path'] in LAYOUT_MODELS:
                check_layout_model(destination)
            elif kind == 'layout' and row['path'] == LAYOUT_PREVIEW:
                check_layout_preview(destination)


def check_release_url(url):
    if not isinstance(url, str) or not url.startswith(RELEASE_PREFIX):
        raise ValueError('Missing or unexpected release URL; no fallback')
    if re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*\.zip',
                    url[len(RELEASE_PREFIX):]) is None:
        raise ValueError('Unexpected release URL')


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


def build(target, assets=None, elegoo_assets=None, *, layout_assets=None, manifest_dir=None):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError('Use a new output directory')
    manifest_dir = Path(manifest_dir) if manifest_dir else ROOT / 'site'
    specs = [('ASSET_BUNDLE.json', 'legacy', assets), ('ELEGOO_ASSET_BUNDLE.json', 'elegoo', elegoo_assets)]
    if os.path.lexists(manifest_dir / LAYOUT_PIN):
        specs.append((LAYOUT_PIN, 'layout', layout_assets))
    elif layout_assets is not None:
        raise ValueError('Layout archive requires LAYOUT_ASSET_BUNDLE.json')
    pins = [(name, kind, offline, *load_pin(manifest_dir / name)) for name, kind, offline in specs]
    for _, kind, _, info, raw in pins:
        if kind == 'layout':
            if len(raw) > MAX_LAYOUT_MANIFEST_BYTES:
                raise ValueError('Oversized layout bundle pin')
            check_layout_pin(info)
    if sum(info['site_bytes'] + len(raw) for _, _, _, info, raw in pins) > MAX_SITE_BYTES:
        raise ValueError('Combined site exceeds size budget')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='assets-build-', dir=target.parent) as temporary:
        folder, bundles = Path(temporary), []
        for index, (name, kind, offline, info, raw) in enumerate(pins):
            archive = acquire(info, offline, folder, f'bundle-{index}.zip')
            records = inspect_archive(archive, info, kind)
            bundles.append((archive, records, name, raw, kind))
        reference_plan = load_versioned_reference_manifest(manifest_dir)
        reference_records = ([{'path': str(PurePosixPath(reference_plan[0]['prefix']) / LAYOUT_DIRECTORY / record['name'])}
                              for record in reference_plan[0]['files']] if reference_plan else [])
        check_collisions([records for _, records, _, _, _ in bundles] + [reference_records],
                         ['ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json', LAYOUT_PIN, REFERENCE_SET])
        stage = folder / 'site'
        stage.mkdir()
        for archive, records, name, raw, kind in bundles:
            extract_verified(archive, stage, records, kind=kind)
            (stage / name).write_bytes(raw)
        reference_files = copy_versioned_references(manifest_dir, stage)
        total = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
        if total > MAX_SITE_BYTES:
            raise ValueError('Site exceeds size budget')
        os.rename(stage, target)
    result = {'files': sum(len(records) for _, records, _, _, _ in bundles) + reference_files, 'site_bytes': total, 'bundles': len(bundles)}
    print(f"Built {result['files']} verified asset files from {len(bundles)} bundles ({total:,} bytes).")
    return result


def load_versioned_reference_manifest(manifest_dir):
    """Validate the optional version pointer before selecting output paths."""
    manifest = manifest_dir / REFERENCE_SET
    if manifest.is_symlink() or getattr(manifest, 'is_junction', lambda: False)():
        raise ValueError('Versioned reference links are forbidden')
    if not os.path.lexists(manifest):
        return None
    raw = read_bounded(manifest, MAX_LAYOUT_MANIFEST_BYTES)
    info = finite_json_bytes(raw)
    if not isinstance(info, dict) or set(info) != {'version', 'prefix', 'files'} or type(info['version']) is not int or info['version'] != 1:
        raise ValueError('Invalid versioned reference manifest')
    records = info['files']
    if not isinstance(records, list) or len(records) != len(REFERENCE_NAMES):
        raise ValueError('Missing versioned reference files')
    for record, name in zip(records, REFERENCE_NAMES):
        if not isinstance(record, dict) or set(record) != {'name', 'bytes', 'sha256'} or record['name'] != name:
            raise ValueError('Unexpected versioned reference file')
        if not valid_count(record['bytes'], positive=True) or record['bytes'] > MAX_LAYOUT_MODEL_BYTES or not valid_sha(record['sha256']):
            raise ValueError('Invalid versioned reference identity')
    tag = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()[:20]
    if info['prefix'] != 'layout-references/' + tag:
        raise ValueError('Versioned reference prefix differs from content identity')
    return info, raw


def copy_versioned_references(manifest_dir, target):
    """Publish one exact version directory while retaining the root bundles."""
    plan = load_versioned_reference_manifest(manifest_dir)
    if plan is None:
        return 0
    info, raw = plan
    records = info['files']
    root = manifest_dir / info['prefix']
    for parent in [manifest_dir, manifest_dir / 'layout-references', root]:
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('Versioned reference links are forbidden')
    expected = {str(PurePosixPath(LAYOUT_DIRECTORY) / name) for name in REFERENCE_NAMES}
    found = set()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            child = Path(folder) / name
            if child.is_symlink() or getattr(child, 'is_junction', lambda: False)():
                raise ValueError('Versioned reference links are forbidden')
        found.update((Path(folder) / name).relative_to(root).as_posix() for name in files)
    if found != expected:
        raise ValueError('Versioned reference directory inventory differs')
    if sum(r['bytes'] for r in records) > MAX_LAYOUT_BYTES:
        raise ValueError('Versioned references exceed size budget')
    total = 0
    for record in records:
        source = root / LAYOUT_DIRECTORY / record['name']
        data = read_bounded(source, record['bytes'])
        if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Versioned reference bytes differ')
        destination = target / info['prefix'] / LAYOUT_DIRECTORY / record['name']
        if not destination.parent.resolve().is_relative_to(target.resolve()):
            raise ValueError('Versioned reference destination escapes output')
        for parent in [destination, *destination.parents]:
            if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
                raise ValueError('Versioned reference destination links are forbidden')
            if parent == target:
                break
        if os.path.lexists(destination):
            raise ValueError('Versioned reference destination collision')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as output:
            output.write(data)
        if record['name'].endswith('.glb.gz'):
            check_layout_model(destination)
        elif record['name'].endswith('.json.gz'):
            check_layout_preview(destination)
        elif record['name'].endswith('.json'):
            pending = [finite_json_bytes(data)]
            while pending:
                value = pending.pop()
                if isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, str):
                    check_layout_text(value, multiline=True)
        else:
            check_layout_text(data.decode('utf-8'), multiline=True)
        total += len(data)
    pointer = target / REFERENCE_SET
    if os.path.lexists(pointer):
        raise ValueError('Versioned reference manifest destination collision')
    with pointer.open('xb') as output:
        output.write(raw)
    return len(records) + 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--assets', type=Path, help='Exact offline original ZIP')
    parser.add_argument('--elegoo-assets', type=Path, help='Exact offline second ELEGOO ZIP')
    parser.add_argument('--layout-assets', type=Path, help='Exact offline optional layout equipment ZIP')
    args = parser.parse_args()
    build(args.output, args.assets, args.elegoo_assets, layout_assets=args.layout_assets)


if __name__ == '__main__':
    main()
