"""Two bounded stdlib-only jobs: exact real bundles and adversarial delivery cases."""
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import pathlib
import shutil
import tempfile
import time
import unittest
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('asset_builder', HERE / 'build_site.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
ARGS = None


def encoded(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_zip(path, entries, records=None):
    if records is None:
        records = [{'path': name, 'bytes': len(data), 'sha256': digest(data)}
                   for name, data in entries.items()]
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.writestr('VIEWER_ASSETS.json', encoded({'files': records}))
    return {'version': 1, 'url': None, 'bytes': path.stat().st_size,
            'sha256': builder.sha(path), 'files': len(records),
            'site_bytes': sum(row['bytes'] for row in records)}


def repack(source, target, *, drop=(), drop_record=True, replace=None, alter_record=None):
    replace = replace or {}
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as output:
        manifest = json.loads(original.read('VIEWER_ASSETS.json'))
        if drop_record:
            manifest['files'] = [row for row in manifest['files'] if row['path'] not in drop]
        for row in manifest['files']:
            if row['path'] in replace:
                data = replace[row['path']]
                row.update(bytes=len(data), sha256=digest(data))
            if alter_record:
                alter_record(row)
        for entry in original.infolist():
            if entry.filename == 'VIEWER_ASSETS.json' or entry.filename in drop:
                continue
            if entry.filename in replace:
                output.writestr(entry.filename, replace[entry.filename])
            else:
                with original.open(entry) as stream, output.open(entry.filename, 'w') as destination:
                    shutil.copyfileobj(stream, destination, 1024 * 1024)
        output.writestr('VIEWER_ASSETS.json', encoded(manifest))
    return {'version': 1, 'url': None, 'bytes': target.stat().st_size,
            'sha256': builder.sha(target), 'files': len(manifest['files']),
            'site_bytes': sum(row['bytes'] for row in manifest['files'])}


class Cases(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bundle-test-', dir=ARGS.work_dir)
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.pins = self.root / 'pins'
        self.pins.mkdir()
        self.old_pin = json.loads((builder.ROOT / 'site/ASSET_BUNDLE.json').read_text(encoding='utf-8'))
        self.new_pin = json.loads(ARGS.elegoo_manifest.read_text(encoding='utf-8'))
        self.save_pins()

    def save_pins(self):
        (self.pins / 'ASSET_BUNDLE.json').write_bytes(encoded(self.old_pin))
        (self.pins / 'ELEGOO_ASSET_BUNDLE.json').write_bytes(encoded(self.new_pin))

    def build(self, old=None, new=None):
        return builder.build(self.root / 'site', old or ARGS.assets,
                             new or ARGS.elegoo_assets, manifest_dir=self.pins)

    def broken_new(self, **changes):
        path = self.root / 'changed.zip'
        pin = repack(ARGS.elegoo_assets, path, **changes)
        return path, pin


class ActualBundles(Cases):
    def test_exact_both_bundles_every_exported_byte(self):
        self.assertEqual(self.old_pin['files'], 22)
        self.assertEqual(self.new_pin['files'], 36)
        result = self.build()
        self.assertEqual(result['files'], 58)
        expected = {'ASSET_BUNDLE.json', 'ELEGOO_ASSET_BUNDLE.json'}
        for archive in (ARGS.assets, ARGS.elegoo_assets):
            with zipfile.ZipFile(archive) as source:
                records = json.loads(source.read('VIEWER_ASSETS.json'))['files']
            for row in records:
                expected.add(row['path'])
                path = self.root / 'site' / row['path']
                self.assertEqual(path.stat().st_size, row['bytes'])
                self.assertEqual(builder.sha(path), row['sha256'])
        actual = {path.relative_to(self.root / 'site').as_posix()
                  for path in (self.root / 'site').rglob('*') if path.is_file()}
        self.assertEqual(actual, expected)
        self.assertLessEqual(result['site_bytes'], 900_000_000)


class Failures(Cases):
    def test_changed_outer_pin_each_bundle(self):
        for kind in ('old', 'new'):
            with self.subTest(bundle=kind):
                saved = self.old_pin if kind == 'old' else self.new_pin
                original = saved['sha256']
                saved['sha256'] = '0' * 64
                self.save_pins()
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    self.build()
                self.assertFalse((self.root / 'site').exists())
                saved['sha256'] = original

    def test_missing_zip_member_without_record_waiver(self):
        path, self.new_pin = self.broken_new(drop=('elegoo-assets/neptune3/model.glb.gz',), drop_record=False)
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.build(new=path)

    def test_missing_runtime_after_repinned_inventory(self):
        path, self.new_pin = self.broken_new(drop=('elegoo-assets/neptune3/model.glb.gz',))
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'runtime/source inventory'):
            self.build(new=path)

    def test_missing_source_after_repinned_inventory(self):
        path, self.new_pin = self.broken_new(drop=('elegoo-sources/neptune3/source/Neptune 3.3DXML',))
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'runtime/source inventory'):
            self.build(new=path)

    def test_missing_license_after_repinned_inventory(self):
        path, self.new_pin = self.broken_new(drop=('elegoo-sources/neptune3/LICENSE',))
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'runtime/source inventory'):
            self.build(new=path)

    def test_license_text_truncated_even_when_repinned(self):
        path, self.new_pin = self.broken_new(replace={'elegoo-sources/neptune3/LICENSE': b'GPL v3'})
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'unchanged license'):
            self.build(new=path)

    def test_original_source_body_changed_even_when_repinned(self):
        path, self.new_pin = self.broken_new(replace={'elegoo-sources/neptune3/source/Neptune 3.3DXML': b'not original source'})
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'original 3DXML'):
            self.build(new=path)

    def test_source_area_must_declare_GPL_even_when_repinned(self):
        path, self.new_pin = self.broken_new(replace={'elegoo-sources/SOURCES.json': encoded(
            {'license': 'NOASSERTION', 'source_scope': 'source_3dxml_tessellation'})})
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'GPL source area'):
            self.build(new=path)

    def test_oversized_source_declaration_is_bounded(self):
        path, self.new_pin = self.broken_new(replace={
            'elegoo-sources/SOURCES.json': b' ' * (builder.MAX_MANIFEST_BYTES + 1)})
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'Oversized ELEGOO source'):
            self.build(new=path)

    def test_individual_file_hash_is_checked(self):
        name = 'elegoo-assets/neptune3/machine_profile.json'
        path, self.new_pin = self.broken_new(alter_record=lambda row: row.update(sha256='0'*64) if row['path'] == name else None)
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'Asset checksum'):
            self.build(new=path)
        self.assertFalse((self.root / 'site').exists())

    def test_path_traversal_absolute_alias_and_windows_paths(self):
        for name in ('../escaped.json', '/escaped.json', 'C:/escaped.json', 'x\\escaped.json',
                     'x/../escaped.json', 'x//escaped.json', './escaped.json', 'x./a.json', 'NUL.json'):
            with self.subTest(path=name):
                archive = self.root / 'bad-path.zip'
                info = write_zip(archive, {name: b'{}'})
                with self.assertRaisesRegex(ValueError, 'path'):
                    builder.inspect_archive(archive, info, 'legacy')
        self.assertFalse((self.root.parent / 'escaped.json').exists())

    def test_cross_bundle_and_casefold_collisions(self):
        for name in ('elegoo-assets/neptune3/machine_profile.json',
                     'ELEGOO-ASSETS/NEPTUNE3/MACHINE_PROFILE.JSON',
                     'elegoo-assets'):
            with self.subTest(path=name):
                archive = self.root / 'collision.zip'
                self.old_pin = write_zip(archive, {name: b'{}'})
                if name == 'elegoo-assets':
                    with self.assertRaisesRegex(ValueError, 'collision'):
                        builder.check_collisions([[{'path': name}], [{'path': 'elegoo-assets/neptune3/a.json'}]], [])
                elif name.endswith('.JSON'):
                    with self.assertRaisesRegex(ValueError, 'collision'):
                        builder.check_collisions([[{'path': name}], [{'path': name.lower()}]], [])
                else:
                    self.save_pins()
                    with self.assertRaisesRegex(ValueError, 'collision'):
                        self.build(old=archive)

    def test_combined_total_budget_includes_both_bundles(self):
        self.old_pin['site_bytes'] = 500_000_000
        self.new_pin['site_bytes'] = 400_000_000
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'Combined site'):
            self.build()

    def test_manifest_total_entry_bytes_and_duplicates(self):
        for change in ('total', 'entry_bytes', 'duplicate'):
            with self.subTest(change=change):
                archive = self.root / 'sizes.zip'
                records = [{'path': 'a.json', 'bytes': 2, 'sha256': digest(b'{}')}]
                if change == 'entry_bytes':
                    records[0]['bytes'] = 3
                if change == 'duplicate':
                    records.append(dict(records[0]))
                info = write_zip(archive, {'a.json': b'{}'}, records)
                if change == 'total':
                    info['site_bytes'] += 1
                with self.assertRaisesRegex(ValueError, 'size|inventory|Duplicate'):
                    builder.inspect_archive(archive, info, 'legacy')

    def test_source_formats_only_in_explicit_GPL_area(self):
        for name in ('run.py', 'models.3dxml', 'elegoo-sources/run.py'):
            with self.subTest(path=name):
                self.assertFalse(builder.allowed_asset(name, 'legacy'))
                self.assertFalse(builder.allowed_asset(name, 'elegoo'))
        self.assertTrue(builder.allowed_asset('elegoo-sources/tools/extract_3dxml.py', 'elegoo'))

    def test_second_manifest_missing_never_skips_bundle(self):
        (self.pins / 'ELEGOO_ASSET_BUNDLE.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_missing_second_zip_or_URL_has_no_fallback(self):
        with self.assertRaises(FileNotFoundError):
            self.build(new=self.root / 'missing.zip')
        with self.assertRaisesRegex(ValueError, 'no fallback'):
            builder.acquire({**self.new_pin, 'url': None}, None, self.root, 'download.zip')

    def test_no_existing_body_count_waiver(self):
        self.old_pin['files'] = 21
        self.save_pins()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.build()


def run_group(case):
    result = unittest.TestResult()
    began = time.monotonic()
    unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
    return {'group': case.__name__, 'tests': result.testsRun,
            'elapsed_seconds': round(time.monotonic()-began, 3),
            'failures': [(str(test), details) for test, details in result.failures],
            'errors': [(str(test), details) for test, details in result.errors]}


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=pathlib.Path, required=True)
    parser.add_argument('--elegoo-assets', type=pathlib.Path, required=True)
    parser.add_argument('--elegoo-manifest', type=pathlib.Path, required=True)
    parser.add_argument('--work-dir', type=pathlib.Path, required=True)
    parser.add_argument('--report', type=pathlib.Path, required=True)
    ARGS = parser.parse_args()
    ARGS.work_dir.mkdir(parents=True, exist_ok=True)
    network_calls = []
    def forbidden_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError('Delivery tests must not access network or use fallback')
    builder.urllib.request.urlopen = forbidden_network
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        groups = list(pool.map(run_group, (ActualBundles, Failures)))
    report = {'status': 'PASS' if all(not g['failures'] and not g['errors'] for g in groups) and not network_calls else 'FAIL',
              'workers': 2, 'native_jobs': 0, 'network_calls': len(network_calls), 'groups': groups}
    ARGS.report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    if report['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
