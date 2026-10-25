"""Versioned reference delivery and rejection fixtures; no network or CAD."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('reference_builder', Path(__file__).with_name('build_site.py'))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class VersionedReferences(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.target = Path(self.temp.name) / 'output'
        self.source.mkdir()
        self.target.mkdir()
        self.records, self.bodies = [], {}
        scene = b'{"asset":{"version":"2.0"}}'
        scene += b' ' * ((-len(scene)) % 4)
        glb = struct.pack('<III', 0x46546c67, 2, 20 + len(scene)) + struct.pack('<II', len(scene), 0x4e4f534a) + scene
        preview = b'{"units":"m","up":"+Y","front":"+Z","rootPivotM":[0,0,0],"geometry":{}}\n'
        for name in builder.REFERENCE_NAMES:
            data = gzip.compress(glb, mtime=0) if name.endswith('.glb.gz') else gzip.compress(preview, mtime=0) if name.endswith('.json.gz') else b'{}\n' if name.endswith('.json') else b'Copyright example; source: https://example.org/\n'
            self.bodies[name] = data
            self.records.append({'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.prefix = 'layout-references/' + hashlib.sha256(json.dumps(self.records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]
        self.directory = self.source / self.prefix / builder.LAYOUT_DIRECTORY
        self.directory.mkdir(parents=True)
        for name, data in self.bodies.items():
            (self.directory / name).write_bytes(data)
        self.manifest = {'version': 1, 'prefix': self.prefix, 'files': self.records}
        self.write()

    def write(self):
        (self.source / builder.REFERENCE_SET).write_bytes(builder.canonical_json(self.manifest))

    def run_copy(self):
        return builder.copy_versioned_references(self.source, self.target)

    def test_exact_delivery_preserves_root_files(self):
        legacy = self.target / 'legacy.bin'
        legacy.write_bytes(b'unchanged')
        self.assertEqual(self.run_copy(), 12)
        self.assertEqual(legacy.read_bytes(), b'unchanged')
        for name, data in self.bodies.items():
            self.assertEqual((self.target / self.prefix / builder.LAYOUT_DIRECTORY / name).read_bytes(), data)

    def test_manifest_absent_keeps_legacy_build(self):
        (self.source / builder.REFERENCE_SET).unlink()
        self.assertEqual(self.run_copy(), 0)

    def test_bundle_version_path_collisions_fail_before_extraction(self):
        full = self.prefix + '/' + builder.LAYOUT_DIRECTORY + '/' + builder.REFERENCE_NAMES[0]
        for collision in [full.upper(), self.prefix.upper(), builder.REFERENCE_SET.lower()]:
            with self.subTest(collision=collision):
                final = self.source.parent / 'final'
                with mock.patch.object(builder, 'load_pin', return_value=({'site_bytes': 1}, b'{}')), \
                     mock.patch.object(builder, 'acquire', return_value=Path('unused.zip')), \
                     mock.patch.object(builder, 'inspect_archive', side_effect=[[{'path': collision}], []]), \
                     mock.patch.object(builder, 'extract_verified') as extract:
                    with self.assertRaisesRegex(ValueError, '[Cc]ollision'):
                        builder.build(final, manifest_dir=self.source)
                    extract.assert_not_called()
                self.assertFalse(final.exists())

    def test_rejects_unknown_fields(self):
        self.manifest['other'] = True
        self.write()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_boolean_version(self):
        self.manifest['version'] = True
        self.write()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_wrong_identity_prefix(self):
        self.manifest['prefix'] = '../outside'
        self.write()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_record_path_injection(self):
        self.manifest['files'][0]['name'] = '../other.glb.gz'
        self.write()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_duplicate_or_missing_inventory(self):
        (self.directory / builder.REFERENCE_NAMES[0]).unlink()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_unlisted_file(self):
        (self.directory / 'unlisted.json').write_bytes(b'{}')
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_changed_bytes(self):
        (self.directory / builder.REFERENCE_NAMES[-1]).write_bytes(b'changed')
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_existing_destination(self):
        self.run_copy()
        with self.assertRaises(ValueError): self.run_copy()

    def test_rejects_duplicate_json_keys(self):
        (self.source / builder.REFERENCE_SET).write_bytes(b'{"version":1,"version":1}')
        with self.assertRaises(ValueError): self.run_copy()

    def test_existing_manifest_is_never_overwritten(self):
        pointer = self.target / builder.REFERENCE_SET
        pointer.write_bytes(b'legacy file')
        with self.assertRaises(ValueError): self.run_copy()
        self.assertEqual(pointer.read_bytes(), b'legacy file')

    def test_private_path_is_rejected_even_with_matching_hash(self):
        name = 'U1_exterior_notices.json'
        data = b'{"nested":{"value":"C:\\\\Users\\\\Example\\\\private.txt"}}'
        (self.directory / name).write_bytes(data)
        record = next(row for row in self.records if row['name'] == name)
        record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        tag = hashlib.sha256(json.dumps(self.records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]
        old = self.source / self.prefix
        self.prefix = 'layout-references/' + tag
        new = self.source / self.prefix
        self.assertTrue(old.resolve().is_relative_to(self.source.resolve()))
        self.assertTrue(new.resolve().is_relative_to(self.source.resolve()))
        old.rename(new)
        self.manifest['prefix'] = self.prefix
        self.write()
        with self.assertRaisesRegex(ValueError, 'local path'): self.run_copy()


if __name__ == '__main__':
    unittest.main()
