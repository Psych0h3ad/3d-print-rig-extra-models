"""Layout equipment delivery fixtures and optional exact-bundle integration checks."""
import argparse
import concurrent.futures
import gzip
import hashlib
import importlib.util
import json
import pathlib
import shutil
import stat
import struct
import tempfile
import time
import unittest
import warnings
import zipfile
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('layout_builder', HERE / 'build_site.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
ARGS = None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def triangle(*, alter=None, decoded_size=None):
    """A self-contained triangle used only as a delivery fixture."""
    positions = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
                'nodes': [{'mesh': 0}], 'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
                'buffers': [{'byteLength': len(positions)}],
                'bufferViews': [{'buffer': 0, 'byteLength': len(positions)}],
                'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3,
                               'type': 'VEC3', 'min': [0, 0, 0], 'max': [1, 1, 0]}]}
    if alter:
        alter(document)
    if decoded_size is not None:
        document['buffers'][0]['byteLength'] = decoded_size
        for _ in range(4):
            payload = json.dumps(document, separators=(',', ':')).encode()
            payload += b' ' * (-len(payload) % 4)
            binary_size = decoded_size - 28 - len(payload)
            if binary_size < len(positions) or binary_size % 4:
                raise ValueError('Fixture decoded size must fit aligned GLB chunks')
            if document['buffers'][0]['byteLength'] == binary_size:
                break
            document['buffers'][0]['byteLength'] = binary_size
        positions += b'\0' * (binary_size - len(positions))
    payload = json.dumps(document, separators=(',', ':')).encode()
    payload += b' ' * (-len(payload) % 4)
    glb = (struct.pack('<4sII', b'glTF', 2, 28 + len(payload) + len(positions))
           + struct.pack('<I4s', len(payload), b'JSON') + payload
           + struct.pack('<I4s', len(positions), b'BIN\0') + positions)
    return gzip.compress(glb, mtime=0)


def fixture_entries():
    return {**{name: triangle() for name in builder.LAYOUT_MODELS},
            builder.LAYOUT_PREVIEW: gzip.compress(b'{"triangle":{"positions":[0,0,0,1,0,0,0,1,0]}}', mtime=0),
            builder.LAYOUT_NOTICE: b'U1 exterior and PopStationMini exterior models.\n'}


def write_bundle(path, entries=None, *, records=None, manifest=None, metadata=None,
                 extra_entries=(), comment=b''):
    entries = fixture_entries() if entries is None else entries
    if records is None:
        records = [{'path': name, 'bytes': len(data), 'sha256': digest(data)}
                   for name, data in sorted(entries.items())]
    raw = builder.canonical_json({'version': 1, 'files': records}) if manifest is None else manifest
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as output:
        for name, data in [*entries.items(), ('VIEWER_ASSETS.json', raw), *extra_entries]:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            if metadata:
                metadata(entry)
            output.writestr(entry, data)
        output.comment = comment
    return {'version': 1, 'url': None, 'bytes': path.stat().st_size,
            'sha256': builder.sha(path), 'files': len(records),
            'site_bytes': sum(row['bytes'] for row in records),
            'manifest_bytes': len(raw), 'manifest_sha256': digest(raw)}


class Fixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='layout-test-', dir=ARGS.work_dir)
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.archive = self.root / 'equipment.zip'
        self.pin = write_bundle(self.archive)

    def inspect(self):
        return builder.inspect_archive(self.archive, self.pin, 'layout')

    def extract(self):
        records = self.inspect()
        target = self.root / 'out'
        target.mkdir()
        builder.extract_verified(self.archive, target, records, kind='layout')
        return target

    def test_positive_exact_six_files_and_byte_hashes(self):
        target = self.extract()
        self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()},
                         builder.LAYOUT_PATHS)
        for name, data in fixture_entries().items():
            self.assertEqual((target / name).read_bytes(), data)

    def test_outer_checksum_and_byte_length(self):
        for field, value in (('sha256', '0' * 64), ('bytes', self.pin['bytes'] + 1)):
            with self.subTest(field=field):
                saved = self.pin[field]
                self.pin[field] = value
                with self.assertRaisesRegex(ValueError, 'Bundle checksum'):
                    self.inspect()
                self.pin[field] = saved

    def test_manifest_checksum_and_exact_byte_length(self):
        for field, value in (('manifest_sha256', '0' * 64),
                             ('manifest_bytes', self.pin['manifest_bytes'] + 1)):
            with self.subTest(field=field):
                saved = self.pin[field]
                self.pin[field] = value
                with self.assertRaisesRegex(ValueError, 'manifest (checksum|size)'):
                    self.inspect()
                self.pin[field] = saved

    def test_each_individual_file_checksum(self):
        for name in sorted(builder.LAYOUT_PATHS):
            with self.subTest(path=name):
                records = builder.json_bytes(builder.canonical_json({'files': self.inspect()}))['files']
                for row in records:
                    if row['path'] == name:
                        row['sha256'] = '0' * 64
                self.pin = write_bundle(self.archive, records=records)
                target = self.root / ('bad-' + pathlib.PurePosixPath(name).name)
                target.mkdir()
                with self.assertRaisesRegex(ValueError, 'Asset checksum'):
                    builder.extract_verified(self.archive, target, self.inspect(), kind='layout')
                self.pin = write_bundle(self.archive)

    def test_every_required_file_missing_after_repin(self):
        for name in sorted(builder.LAYOUT_PATHS):
            with self.subTest(path=name):
                entries = fixture_entries()
                del entries[name]
                self.pin = write_bundle(self.archive, entries)
                with self.assertRaisesRegex(ValueError, 'pin|inventory'):
                    self.inspect()

    def test_member_missing_without_manifest_waiver(self):
        records = self.inspect()
        entries = fixture_entries()
        entries.pop(next(iter(builder.LAYOUT_MODELS)))
        self.pin = write_bundle(self.archive, entries, records=records)
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.inspect()

    def test_extra_files_after_repin(self):
        for name in ('layout/assets/equipment/u1-exterior/extra.glb.gz', 'index.html',
                     'layout/assets/equipment/u1-exterior/metadata.json'):
            with self.subTest(path=name):
                self.pin = write_bundle(self.archive, {**fixture_entries(), name: b'{}'})
                with self.assertRaisesRegex(ValueError, 'pin|inventory'):
                    self.inspect()

    def test_replacement_paths_traversal_alias_case_and_reserved(self):
        for name in ('../outside.txt', '/outside.txt', 'C:/outside.txt', 'x\\outside.txt',
                     'x/../outside.txt', 'x//outside.txt', './outside.txt', 'NUL.txt',
                     'layout./outside.txt', builder.LAYOUT_NOTICE.upper(), 'index.html',
                     builder.LAYOUT_DIRECTORY + '/other.glb.gz'):
            with self.subTest(path=name):
                entries = fixture_entries()
                entries[name] = entries.pop(builder.LAYOUT_NOTICE)
                self.pin = write_bundle(self.archive, entries)
                with self.assertRaises(ValueError):
                    self.inspect()
        self.assertFalse((self.root.parent / 'outside.txt').exists())

    def test_duplicate_manifest_record(self):
        records = self.inspect()
        records[-1] = dict(records[0])
        self.pin = write_bundle(self.archive, records=records)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.inspect()

    def test_duplicate_zip_member(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            self.pin = write_bundle(self.archive, extra_entries=((builder.LAYOUT_NOTICE, b'other'),))
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.inspect()

    def test_casefold_parent_and_pin_collisions(self):
        for name in (builder.LAYOUT_NOTICE, builder.LAYOUT_NOTICE.upper(),
                     'layout', builder.LAYOUT_DIRECTORY):
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'collision'):
                builder.check_collisions([[{'path': name}], self.inspect()], [builder.LAYOUT_PIN])
        for name in (builder.LAYOUT_PIN, builder.LAYOUT_PIN.lower()):
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'collision'):
                builder.check_collisions([[{'path': name}], self.inspect()], [builder.LAYOUT_PIN])

    def test_zip_symlink_special_file_and_directory(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR):
            with self.subTest(mode=mode):
                self.pin = write_bundle(self.archive, metadata=lambda e: setattr(e, 'external_attr', mode << 16))
                with self.assertRaisesRegex(ValueError, 'ZIP entry'):
                    self.inspect()

    def test_encrypted_manifest_and_model_before_read(self):
        for name in ('VIEWER_ASSETS.json', sorted(builder.LAYOUT_MODELS)[0]):
            with self.subTest(path=name):
                self.pin = write_bundle(self.archive)
                data = bytearray(self.archive.read_bytes())
                offset = 0
                while True:
                    offset = data.find(b'PK\x01\x02', offset)
                    if offset < 0:
                        break
                    name_size = struct.unpack_from('<H', data, offset + 28)[0]
                    if data[offset + 46:offset + 46 + name_size].decode() == name:
                        data[offset + 8] |= 1
                        break
                    offset += 46 + name_size
                self.archive.write_bytes(data)
                self.pin.update(bytes=len(data), sha256=digest(data))
                with self.assertRaisesRegex(ValueError, 'ZIP entry'):
                    self.inspect()

    def test_zip_comments_and_extra_fields(self):
        self.pin = write_bundle(self.archive, comment=b'equipment')
        with self.assertRaisesRegex(ValueError, 'comment'):
            self.inspect()
        for field, value in (('comment', b'equipment'), ('extra', b'\x00\x00\x00\x00'),
                             ('compress_type', zipfile.ZIP_DEFLATED)):
            with self.subTest(field=field):
                self.pin = write_bundle(self.archive, metadata=lambda e: setattr(e, field, value))
                with self.assertRaisesRegex(ValueError, 'ZIP entry'):
                    self.inspect()

    def test_manifest_canonical_schema_order_and_duplicate_keys(self):
        records = self.inspect()
        variants = [b'{"version":1,"version":1,"files":[]}',
                    builder.canonical_json({'version': 1, 'files': records, 'details': {}}),
                    builder.canonical_json({'version': True, 'files': records}),
                    builder.canonical_json({'version': 1, 'files': list(reversed(records))}),
                    json.dumps({'version': 1, 'files': records}, indent=2).encode(),
                    builder.canonical_json({'version': 1, 'files': [None] * len(builder.LAYOUT_PATHS)}),
                    builder.canonical_json({'version': 1, 'files': [{**row, 'details': ''} for row in records]})]
        for raw in variants:
            with self.subTest(manifest=raw[:60]):
                self.pin = write_bundle(self.archive, records=records, manifest=raw)
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_pin_schema_and_limits(self):
        for key, value in (('files', 4), ('details', {}), ('manifest_sha256', 'bad'),
                           ('manifest_bytes', True), ('manifest_bytes', builder.MAX_LAYOUT_MANIFEST_BYTES + 1),
                           ('bytes', builder.MAX_LAYOUT_ARCHIVE_BYTES + 1),
                           ('site_bytes', builder.MAX_LAYOUT_BYTES + 1)):
            with self.subTest(field=key):
                pin = {**self.pin, key: value}
                with self.assertRaisesRegex(ValueError, 'pin'):
                    builder.check_layout_pin(pin)

    def test_individual_and_total_asset_limits(self):
        records = self.inspect()
        for value in (0, True, builder.MAX_LAYOUT_MODEL_BYTES + 1):
            with self.subTest(bytes=value):
                changed = [dict(row) for row in records]
                changed[1]['bytes'] = value
                self.pin = write_bundle(self.archive, records=changed)
                with self.assertRaisesRegex(ValueError, 'size|checksum'):
                    self.inspect()

    def test_notice_text_and_size(self):
        for data in (b'', b' ', b'\xff', b'x\x00y', '\u202e'.encode(), b'C:\\example\\item',
                     b'D:/example/item', b'\\\\example\\share\\item', b'//example/share/item',
                     b'file:///example/item', b'/home/example/item', b'/Users/example/item',
                     b'x' * (builder.MAX_LAYOUT_NOTICE_BYTES + 1)):
            with self.subTest(notice=data[:40]):
                self.pin = write_bundle(self.archive, {**fixture_entries(), builder.LAYOUT_NOTICE: data})
                with self.assertRaises((ValueError, UnicodeError)):
                    self.inspect()

    def test_notice_public_license_source_and_copyright_survive_verbatim(self):
        data = ('Copyright \u00a9 2026 Example Contributors. All rights reserved.\n'
                'License: https://creativecommons.org/licenses/by/4.0/\n'
                'Source: https://example.org/models/u1/exterior?revision=1\n'
                'U1 exterior / PopStationMini exterior.\n').encode('utf-8')
        self.pin = write_bundle(self.archive, {**fixture_entries(), builder.LAYOUT_NOTICE: data})
        self.assertEqual((self.extract() / builder.LAYOUT_NOTICE).read_bytes(), data)

    def test_model_encoded_16_mib_boundary_and_manifest_limit(self):
        limit = 16 * 1024 * 1024
        self.assertEqual(builder.MAX_LAYOUT_MODEL_BYTES, limit)
        self.assertEqual(builder.MAX_LAYOUT_GLB_BYTES, limit)
        self.assertEqual(builder.MAX_LAYOUT_BYTES, 64_000_000)
        data = triangle()
        path = self.root / 'encoded-boundary.glb.gz'
        # Gzip permits zero padding after its complete member.
        for size in (limit - 1, limit, limit + 1):
            with self.subTest(encoded_bytes=size):
                path.write_bytes(data + b'\0' * (size - len(data)))
                if size <= limit:
                    builder.check_layout_model(path)
                else:
                    with self.assertRaisesRegex(ValueError, 'encoded layout GLB size'):
                        builder.check_layout_model(path)
        records = self.inspect()
        for name in sorted(builder.LAYOUT_MODELS):
            with self.subTest(path=name):
                changed = [{**row, 'bytes': limit + 1} if row['path'] == name else dict(row)
                           for row in records]
                self.pin = write_bundle(self.archive, records=changed)
                with self.assertRaisesRegex(ValueError, 'layout asset size'):
                    self.inspect()

    def test_model_decoded_16_mib_boundary(self):
        limit = 16 * 1024 * 1024
        path = self.root / 'decoded-boundary.glb.gz'
        for size in (limit - 4, limit, limit + 4):
            with self.subTest(decoded_bytes=size):
                data = triangle(decoded_size=size)
                self.assertEqual(len(gzip.decompress(data)), size)
                path.write_bytes(data)
                if size <= limit:
                    builder.check_layout_model(path)
                else:
                    with self.assertRaisesRegex(ValueError, 'GLB header or size'):
                        builder.check_layout_model(path)
        raw = bytearray(gzip.decompress(triangle(decoded_size=limit)))
        raw.append(0)
        struct.pack_into('<I', raw, 8, len(raw))
        path.write_bytes(gzip.compress(raw, mtime=0))
        with self.assertRaisesRegex(ValueError, 'GLB header or size'):
            builder.check_layout_model(path)

    def test_bad_gzip_glb_length_header_and_expansion(self):
        name = sorted(builder.LAYOUT_MODELS)[0]
        raw = gzip.decompress(triangle())
        variants = [b'not gzip', gzip.compress(b'not a GLB', mtime=0),
                    gzip.compress(b'bad!' + raw[4:], mtime=0),
                    gzip.compress(raw[:8] + struct.pack('<I', len(raw) - 1) + raw[12:], mtime=0),
                    gzip.compress(raw[:8] + struct.pack('<I', builder.MAX_LAYOUT_GLB_BYTES + 1) + raw[12:], mtime=0),
                    triangle()[:-4]]
        for index, data in enumerate(variants):
            with self.subTest(variant=index):
                self.pin = write_bundle(self.archive, {**fixture_entries(), name: data})
                target = self.root / f'bad-glb-{index}'
                target.mkdir()
                with self.assertRaises((ValueError, OSError, EOFError)):
                    builder.extract_verified(self.archive, target, self.inspect(), kind='layout')

    def test_preview_json_is_bounded_finite_dictionary_data(self):
        for index, raw in enumerate((b'[]', b'null', b'{', b'{"x":1,"x":2}',
                                      b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'\xff',
                                      b' ' * (builder.MAX_LAYOUT_PREVIEW_BYTES + 1))):
            with self.subTest(preview=index):
                path = self.root / f'preview-{index}.gz'
                path.write_bytes(gzip.compress(raw, mtime=0))
                with self.assertRaises((ValueError, UnicodeError)):
                    builder.check_layout_preview(path)
        path = self.root / 'preview-data.gz'
        text = '__import__("os").system("this is data")'
        path.write_bytes(gzip.compress(json.dumps({'text': text}).encode(), mtime=0))
        self.assertEqual(builder.check_layout_preview(path), {'text': text})

    def test_preview_encoded_limit_and_gzip_integrity(self):
        records = self.inspect()
        for row in records:
            if row['path'] == builder.LAYOUT_PREVIEW:
                row['bytes'] = builder.MAX_LAYOUT_PREVIEW_BYTES + 1
        self.pin = write_bundle(self.archive, records=records)
        with self.assertRaisesRegex(ValueError, 'asset size'):
            self.inspect()
        for index, data in enumerate((b'not gzip', gzip.compress(b'{"ok":true}', mtime=0)[:-4])):
            self.pin = write_bundle(self.archive, {**fixture_entries(), builder.LAYOUT_PREVIEW: data})
            target = self.root / f'bad-preview-{index}'
            target.mkdir()
            with self.assertRaises((ValueError, OSError, EOFError)):
                builder.extract_verified(self.archive, target, self.inspect(), kind='layout')

    def test_preview_raw_limit_is_inclusive(self):
        path = self.root / 'preview-boundary.gz'
        raw = b'{"ok":true}'
        path.write_bytes(gzip.compress(raw + b' ' * (builder.MAX_LAYOUT_PREVIEW_BYTES - len(raw)), mtime=0))
        self.assertEqual(builder.check_layout_preview(path), {'ok': True})

    def test_unpinned_offline_layout_is_rejected(self):
        pins = self.root / 'pins'
        pins.mkdir()
        with self.assertRaisesRegex(ValueError, 'requires LAYOUT'):
            builder.build(self.root / 'site', layout_assets=self.archive, manifest_dir=pins)
        self.assertFalse((self.root / 'site').exists())

    def test_invalid_pin_is_not_treated_as_absent(self):
        pins = self.root / 'pins'
        pins.mkdir()
        for name in ('ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json'):
            shutil.copyfile(builder.ROOT / 'site' / name, pins / name)
        for raw in (b'{}', b'null', b'[]', b'{', b'{"version":1,"version":1}'):
            with self.subTest(pin=raw):
                (pins / builder.LAYOUT_PIN).write_bytes(raw)
                with mock.patch.object(builder, 'acquire', side_effect=AssertionError('Unexpected acquisition')):
                    with self.assertRaises(ValueError):
                        builder.build(self.root / 'site', manifest_dir=pins)
                self.assertFalse((self.root / 'site').exists())

    def test_layout_pin_limit_is_checked_before_acquisition(self):
        pins = self.root / 'pins'
        pins.mkdir()
        for name in ('ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json'):
            shutil.copyfile(builder.ROOT / 'site' / name, pins / name)
        (pins / builder.LAYOUT_PIN).write_bytes(builder.canonical_json(self.pin)
                                               + b' ' * builder.MAX_LAYOUT_MANIFEST_BYTES)
        with mock.patch.object(builder, 'acquire', side_effect=AssertionError('Unexpected acquisition')):
            with self.assertRaisesRegex(ValueError, 'Oversized layout bundle pin'):
                builder.build(self.root / 'site', manifest_dir=pins)

    def test_combined_budget_includes_third_pin_bytes(self):
        pins = self.root / 'pins'
        pins.mkdir()
        remaining = builder.MAX_SITE_BYTES - self.pin['site_bytes']
        for index, name in enumerate(('ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json')):
            info, _ = builder.load_pin(builder.ROOT / 'site' / name)
            info['site_bytes'] = remaining // 2 + (remaining % 2 if index == 0 else 0)
            (pins / name).write_bytes(builder.canonical_json(info))
        (pins / builder.LAYOUT_PIN).write_bytes(builder.canonical_json(self.pin))
        with mock.patch.object(builder, 'acquire', side_effect=AssertionError('Unexpected acquisition')):
            with self.assertRaisesRegex(ValueError, 'Combined site'):
                builder.build(self.root / 'site', layout_assets=self.archive, manifest_dir=pins)

    def test_url_validation_and_no_fallback(self):
        valid = builder.RELEASE_PREFIX + 'equipment-v1/equipment.zip'
        builder.check_release_url(valid)
        for url in (None, 'https://example.invalid/equipment.zip', valid + '?x=1',
                    valid + '#x', builder.RELEASE_PREFIX + '../equipment.zip',
                    builder.RELEASE_PREFIX + '%2e%2e/equipment.zip'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                builder.check_release_url(url)
        with self.assertRaisesRegex(ValueError, 'no fallback'):
            builder.acquire(self.pin, None, self.root, 'download.zip')


class ActualBundles(Fixtures):
    # Only the explicitly selected integration methods run for this class.
    def test_actual_two_and_three_bundles_preserve_all_existing_bytes(self):
        pins = self.root / 'pins'
        pins.mkdir()
        existing = {}
        for name, archive in (('ASSET_BUNDLE.json', ARGS.assets),
                              ('ELEGOO_ASSET_BUNDLE.json', ARGS.elegoo_assets)):
            shutil.copyfile(builder.ROOT / 'site' / name, pins / name)
            with zipfile.ZipFile(archive) as source:
                existing.update({row['path']: row for row in json.loads(source.read('VIEWER_ASSETS.json'))['files']})
        outputs = []
        for count in (2, 3):
            if count == 3:
                (pins / builder.LAYOUT_PIN).write_bytes(builder.canonical_json(self.pin))
            target = self.root / f'site-{count}'
            result = builder.build(target, ARGS.assets, ARGS.elegoo_assets, manifest_dir=pins,
                                   layout_assets=self.archive if count == 3 else None)
            self.assertEqual(result['bundles'], count)
            self.assertEqual(result['files'], 58 + (len(builder.LAYOUT_PATHS) if count == 3 else 0))
            expected = set(existing) | {'ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json'}
            if count == 3:
                expected |= builder.LAYOUT_PATHS | {builder.LAYOUT_PIN}
            actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
            self.assertEqual(actual, expected)
            for name, row in existing.items():
                self.assertEqual((target / name).stat().st_size, row['bytes'])
                self.assertEqual(builder.sha(target / name), row['sha256'])
            for name in ('ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json'):
                self.assertEqual((target / name).read_bytes(), (builder.ROOT / 'site' / name).read_bytes())
            outputs.append(target)
        self.assertEqual((outputs[1] / builder.LAYOUT_PIN).read_bytes(), builder.canonical_json(self.pin))
        with self.assertRaisesRegex(ValueError, 'new output'):
            builder.build(outputs[1], ARGS.assets, ARGS.elegoo_assets, manifest_dir=pins,
                          layout_assets=self.archive)
        # The same malformed member must leave no partial published output.
        entries = fixture_entries()
        entries[sorted(builder.LAYOUT_MODELS)[0]] = b'not gzip'
        self.pin = write_bundle(self.archive, entries)
        (pins / builder.LAYOUT_PIN).write_bytes(builder.canonical_json(self.pin))
        with self.assertRaises((ValueError, OSError)):
            builder.build(self.root / 'failed-site', ARGS.assets, ARGS.elegoo_assets,
                          manifest_dir=pins, layout_assets=self.archive)
        self.assertFalse((self.root / 'failed-site').exists())
        self.assertFalse(list(self.root.glob('assets-build-*')))

    def test_actual_second_bundle_and_layout_collision_fail_before_output(self):
        pins = self.root / 'pins'
        pins.mkdir()
        legacy = self.root / 'collision.zip'
        legacy_pin = write_bundle(legacy)
        (pins / 'ASSET_BUNDLE.json').write_bytes(builder.canonical_json(legacy_pin))
        shutil.copyfile(builder.ROOT / 'site/ELEGOO_ASSET_BUNDLE.json', pins / 'ELEGOO_ASSET_BUNDLE.json')
        (pins / builder.LAYOUT_PIN).write_bytes(builder.canonical_json(self.pin))
        with self.assertRaisesRegex(ValueError, 'collision'):
            builder.build(self.root / 'site', legacy, ARGS.elegoo_assets,
                          manifest_dir=pins, layout_assets=self.archive)
        self.assertFalse((self.root / 'site').exists())


def run_group(named_suite):
    name, suite = named_suite
    result = unittest.TestResult()
    began = time.monotonic()
    suite.run(result)
    return {'group': name, 'tests': result.testsRun, 'elapsed_seconds': round(time.monotonic() - began, 3),
            'failures': [(str(test), details) for test, details in result.failures],
            'errors': [(str(test), details) for test, details in result.errors]}


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=pathlib.Path)
    parser.add_argument('--elegoo-assets', type=pathlib.Path)
    parser.add_argument('--work-dir', type=pathlib.Path, required=True)
    parser.add_argument('--report', type=pathlib.Path, required=True)
    ARGS = parser.parse_args()
    if bool(ARGS.assets) != bool(ARGS.elegoo_assets):
        parser.error('Supply both exact existing archives for integration checks')
    ARGS.work_dir.mkdir(parents=True, exist_ok=True)
    suites = [('Fixtures', unittest.defaultTestLoader.loadTestsFromTestCase(Fixtures))]
    if ARGS.assets:
        suites.append(('ActualBundles', unittest.TestSuite([
            ActualBundles('test_actual_two_and_three_bundles_preserve_all_existing_bytes'),
            ActualBundles('test_actual_second_bundle_and_layout_collision_fail_before_output')])))
    def forbidden_network(*args, **kwargs):
        raise AssertionError('Delivery tests must not access network or use fallback')
    builder.urllib.request.urlopen = forbidden_network
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        groups = list(pool.map(run_group, suites))
    report = {'status': 'PASS' if all(not g['errors'] and not g['failures'] for g in groups) else 'FAIL',
              'workers': 2, 'actual_existing_bundles': bool(ARGS.assets),
              'final_layout_models': 'not supplied', 'groups': groups}
    ARGS.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    if report['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
