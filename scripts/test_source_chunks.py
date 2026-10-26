"""Exact compressed delivery, decoded integrity and hostile archive fixtures."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import source_chunks as chunks


class SourceChunks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / 'site'
        self.site.mkdir()
        self.target = self.root / 'target'
        self.target.mkdir()
        self.bodies = {'NOTICE.txt': b'Example author. Source: https://example.org/model\n', 'SOURCES.json': b'{"source":"https://example.org/model"}\n'}
        raw = b'Actual geometry fixture' * 32
        self.raw_hash = hashlib.sha256(raw).hexdigest()
        self.chunk_name = self.raw_hash + '.bin.gz'
        self.bodies[self.chunk_name] = gzip.compress(raw, mtime=0)
        self.rows = [{'name': n, 'bytes': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n, b in self.bodies.items()]
        self.rows[-1].update(raw_bytes=len(raw), raw_sha256=self.raw_hash)
        self.model = '1' * 64
        self.info = dict(version=1, model_sha256=self.model, prefix='source-assemblies/' + self.model,
                         url=chunks.RELEASE + 'fixture/source.zip', bytes=1, sha256='0' * 64, files=self.rows)
        self.archive = self.root / 'source.zip'
        self.write_archive()

    def write_archive(self, extra=None):
        with zipfile.ZipFile(self.archive, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, body in self.bodies.items(): archive.writestr(name, body)
            if extra: archive.writestr(extra, b'unknown')
        self.info.update(bytes=self.archive.stat().st_size, sha256=chunks.digest(self.archive))
        self.write_pin()

    def write_pin(self):
        (self.site / chunks.PIN).write_text(json.dumps(self.info), encoding='utf-8')

    def install(self):
        return chunks.install(chunks.load_plan(self.site), self.root, self.target, self.archive)

    def replace_payload(self, name, body):
        self.bodies[name] = body
        row = next(row for row in self.rows if row['name'] == name)
        row.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        self.write_archive()

    def add_payload(self, name, body, raw=None):
        self.bodies[name] = body
        row = {'name': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        if raw is not None:
            row.update(raw_bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest())
        self.rows.append(row)
        return row

    def test_exact_compressed_and_decoded_bytes(self):
        self.assertEqual(self.install(), 4)
        for name, body in self.bodies.items():
            self.assertEqual((self.target / self.info['prefix'] / name).read_bytes(), body)

    def test_absent_pin_does_not_change_existing_delivery(self):
        (self.site / chunks.PIN).unlink()
        self.assertEqual(chunks.install(chunks.load_plan(self.site), self.root, self.target), 0)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_rejects_corrupted_encoded_chunk(self):
        self.bodies[self.chunk_name] += b'alteration'
        self.write_archive()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_wrong_decoded_size(self):
        self.rows[-1]['raw_bytes'] -= 1
        self.write_pin()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_wrong_decoded_hash(self):
        old = self.chunk_name
        self.chunk_name = '2' * 64 + '.bin.gz'
        self.bodies[self.chunk_name] = self.bodies.pop(old)
        self.rows[-1].update(name=self.chunk_name, raw_sha256='2' * 64)
        self.write_archive()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_additional_or_traversal_members(self):
        for name in ('extra.bin', '../outside.bin'):
            with self.subTest(name=name):
                self.write_archive(name)
                with self.assertRaises(ValueError): self.install()
                self.assertEqual(list(self.target.iterdir()), [])

    def test_rejects_duplicate_members(self):
        with zipfile.ZipFile(self.archive, 'a') as archive:
            archive.writestr('NOTICE.txt', self.bodies['NOTICE.txt'])
        self.info.update(bytes=self.archive.stat().st_size, sha256=chunks.digest(self.archive))
        self.write_pin()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_missing_attribution(self):
        self.info['files'] = self.rows[1:]
        self.write_pin()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_changed_prefix_and_host(self):
        for key, value in [('prefix', '../outside'), ('url', 'https://example.org/source.zip')]:
            original = self.info[key]
            self.info[key] = value
            self.write_pin()
            with self.assertRaises(ValueError): self.install()
            self.info[key] = original

    def test_rejects_local_paths_in_notices(self):
        self.bodies['NOTICE.txt'] = b'Source C:/private/model.step'
        self.rows[0].update(bytes=len(self.bodies['NOTICE.txt']), sha256=hashlib.sha256(self.bodies['NOTICE.txt']).hexdigest())
        self.write_archive()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_unix_notice_with_matching_hashes(self):
        self.replace_payload('NOTICE.txt', b'Source /home/example/private/model.step\n')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_rejects_nested_unix_json_with_matching_hashes(self):
        self.replace_payload('SOURCES.json', b'{"sources":[{"source":"/Users/example/private/model.step"}]}')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_rejects_unicode_escaped_windows_json_with_matching_hashes(self):
        self.replace_payload('SOURCES.json', br'{"sources":[{"source":"\u0043:\u002fUsers\u002fexample\u002fmodel.step"}]}')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_rejects_unicode_escaped_file_uri_json_with_matching_hashes(self):
        self.replace_payload('SOURCES.json', br'{"sources":[{"source":"\u0066ile:\u002f\u002f\u002fC:\u002fUsers\u002fexample"}]}')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_rejects_unicode_escaped_local_path_in_nested_json_key(self):
        self.replace_payload('SOURCES.json', br'{"sources":[{"\u0043:\u002fUsers\u002fexample\u002fmodel.step":"author"}]}')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_rejects_local_path_as_nested_json_array_value(self):
        self.replace_payload('SOURCES.json', b'{"sources":[["/home/example/private/model.step"]]}')
        with self.assertRaisesRegex(ValueError, 'Non-public path'):
            self.install()

    def test_accepts_public_urls_in_notices_and_decoded_json(self):
        notice = b'Source https://example.org/home/model.step\nLicense https://example.org/Users/license\n'
        sources = br'{"https://example.org/home/model.step":[{"source":"https:\/\/example.org\/Users\/model.step","related":["https://example.org/var/model.step"]}]}'
        self.replace_payload('NOTICE.txt', notice)
        self.replace_payload('SOURCES.json', sources)
        self.assertEqual(self.install(), 4)
        self.assertEqual((self.target / self.info['prefix'] / 'NOTICE.txt').read_bytes(), notice)
        self.assertEqual((self.target / self.info['prefix'] / 'SOURCES.json').read_bytes(), sources)

    def test_mixed_42_gzip_2_raw_and_external_metadata_preserve_exact_bytes(self):
        for i in range(41):
            raw = b'Synthetic chunk\x00\xff' + bytes([i])
            name = hashlib.sha256(raw).hexdigest() + '.bin.gz'
            self.add_payload(name, gzip.compress(raw, mtime=0), raw)
        for i in range(2):
            raw = b'Uncompressed synthetic chunk\xff\xfe' + bytes([i])
            self.add_payload(hashlib.sha256(raw).hexdigest() + '.bin', raw)
        document = b'{"sources":[{"url":"https://example.org/public"}]}'
        for name, size in ((chunks.INVENTORY, 2_700_000), ('manifest-' + self.model + '.json', 4_000_000)):
            self.add_payload(name, document + b' ' * (size - len(document)))
        self.write_archive()
        self.assertEqual(len(self.rows), 48)
        self.assertEqual(self.install(), 49)
        destination = self.target / self.info['prefix']
        self.assertEqual({p.name for p in destination.iterdir()}, set(self.bodies))
        for row in self.rows:
            body = (destination / row['name']).read_bytes()
            self.assertEqual(body, self.bodies[row['name']])
            if row['name'].endswith('.bin.gz'):
                decoded = gzip.decompress(body)
                self.assertEqual(len(decoded), row['raw_bytes'])
                self.assertEqual(hashlib.sha256(decoded).hexdigest(), row['raw_sha256'])

    def test_rejects_raw_chunk_name_not_matching_its_hash(self):
        self.add_payload('2' * 64 + '.bin', b'raw geometry')
        self.write_archive()
        with self.assertRaisesRegex(ValueError, 'raw chunk identity'):
            self.install()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_rejects_corrupted_raw_chunk_even_with_repinned_archive(self):
        raw = b'raw geometry'
        name = hashlib.sha256(raw).hexdigest() + '.bin'
        self.add_payload(name, raw)
        self.bodies[name] = b'RAW geometry'
        self.write_archive()
        with self.assertRaisesRegex(ValueError, 'Chunk identity differs'):
            self.install()

    def test_raw_records_do_not_accept_gzip_only_fields(self):
        raw = b'raw geometry'
        self.add_payload(hashlib.sha256(raw).hexdigest() + '.bin', raw, raw)
        self.write_archive()
        with self.assertRaisesRegex(ValueError, 'chunk record'):
            chunks.load_plan(self.site)

    def test_rejects_arbitrary_metadata_names_and_wrong_model_identity(self):
        row = self.add_payload(chunks.INVENTORY, b'{}')
        for name in ('other.json', 'inventory-rh3d_e3ng_v12_300.json',
                     'manifest-' + '2' * 64 + '.json', '../' + chunks.INVENTORY,
                     'nested/' + chunks.INVENTORY, 'SOURCES.JSON'):
            with self.subTest(name=name):
                row['name'] = name
                self.write_pin()
                with self.assertRaises(ValueError):
                    chunks.load_plan(self.site)

    def test_json_8_mib_and_notice_1_mib_plan_limits_are_inclusive(self):
        for name in (chunks.INVENTORY, 'manifest-' + self.model + '.json'):
            self.add_payload(name, b'{}')
        for row in self.rows:
            if row['name'].endswith('.json') or row['name'] == 'NOTICE.txt':
                limit = chunks.MAX_JSON if row['name'].endswith('.json') else chunks.MAX_NOTICE
                original = row['bytes']
                with self.subTest(name=row['name']):
                    row['bytes'] = limit
                    self.write_pin()
                    self.assertIsNotNone(chunks.load_plan(self.site))
                    row['bytes'] = limit + 1
                    self.write_pin()
                    with self.assertRaises(ValueError):
                        chunks.load_plan(self.site)
                row['bytes'] = original

    def test_all_json_metadata_rejects_decoded_private_keys_and_values(self):
        leaks = (
            br'{"sources":[{"\u0043:\u002fUsers\u002fexample":"https://example.org/public"}]}',
            br'{"sources":[{"values":["\u002fhome\u002fexample\u002fmodel.step"]}]}',
        )
        for i, name in enumerate(('SOURCES.json', chunks.INVENTORY, 'manifest-' + self.model + '.json')):
            for j, leaked in enumerate(leaks):
                with self.subTest(name=name, leak=j):
                    # Separate targets ensure rejection comes from the payload, not a prior failed install.
                    self.target = self.root / ('private-json-' + str(i) + '-' + str(j))
                    self.target.mkdir()
                    if name not in self.bodies:
                        self.add_payload(name, b'{}')
                    self.replace_payload(name, leaked)
                    with self.assertRaisesRegex(ValueError, 'Non-public path'):
                        self.install()
                    self.replace_payload(name, b'{}')

    def test_rejects_invalid_json_in_both_external_metadata_files(self):
        for i, name in enumerate((chunks.INVENTORY, 'manifest-' + self.model + '.json')):
            with self.subTest(name=name):
                self.target = self.root / ('invalid-json-' + str(i))
                self.target.mkdir()
                self.add_payload(name, b'{"unclosed":')
                self.write_archive()
                with self.assertRaises(json.JSONDecodeError):
                    self.install()
                self.replace_payload(name, b'{}')

    def test_raw_plus_decoded_gzip_total_512_mib_is_inclusive(self):
        # Metadata-only declarations exercise the cap without allocating a 512 MiB fixture.
        self.rows[-1]['raw_bytes'] = chunks.MAX_CHUNK
        for i in range(1, 31):
            raw_hash = hashlib.sha256(('budget-' + str(i)).encode()).hexdigest()
            self.rows.append(dict(name=raw_hash + '.bin.gz', bytes=1, sha256='0' * 64,
                                  raw_bytes=chunks.MAX_CHUNK, raw_sha256=raw_hash))
        self.rows.append(dict(name='2' * 64 + '.bin', bytes=chunks.MAX_CHUNK - 1, sha256='2' * 64))
        self.rows.append(dict(name='3' * 64 + '.bin', bytes=1, sha256='3' * 64))
        self.write_pin()
        self.assertIsNotNone(chunks.load_plan(self.site))
        self.rows[-1]['bytes'] = 2
        self.write_pin()
        with self.assertRaisesRegex(ValueError, 'oversized chunk set'):
            chunks.load_plan(self.site)

    def test_rejects_oversized_raw_chunk_and_total_encoded_set(self):
        row = dict(name='2' * 64 + '.bin', bytes=chunks.MAX_CHUNK + 1, sha256='2' * 64)
        self.rows.append(row)
        self.write_pin()
        with self.assertRaises(ValueError):
            chunks.load_plan(self.site)
        row['bytes'] = chunks.MAX_CHUNK
        for i in range(7):
            key = hashlib.sha256(('encoded-' + str(i)).encode()).hexdigest()
            self.rows.append(dict(name=key + '.bin', bytes=chunks.MAX_CHUNK, sha256=key))
        self.write_pin()
        with self.assertRaisesRegex(ValueError, 'oversized chunk set'):
            chunks.load_plan(self.site)

    def test_external_metadata_does_not_replace_required_notices_or_geometry(self):
        self.add_payload(chunks.INVENTORY, b'{}')
        saved = self.info['files']
        for missing in ('NOTICE.txt', 'SOURCES.json', self.chunk_name):
            with self.subTest(missing=missing):
                self.info['files'] = [row for row in saved if row['name'] != missing]
                self.write_pin()
                with self.assertRaises(ValueError):
                    chunks.load_plan(self.site)
        self.info['files'] = saved

    def test_rejects_repeated_installation(self):
        self.install()
        with self.assertRaises(ValueError): self.install()

    def test_rejects_nonfinite_json_numbers_in_every_metadata_file(self):
        for i, name in enumerate(('SOURCES.json', chunks.INVENTORY, 'manifest-' + self.model + '.json')):
            if name not in self.bodies:
                self.add_payload(name, b'{}')
            for j, number in enumerate(('NaN', 'Infinity', '-Infinity', '1e999')):
                with self.subTest(name=name, number=number):
                    self.target = self.root / ('nonfinite-' + str(i) + '-' + str(j))
                    self.target.mkdir()
                    self.replace_payload(name, ('{"nested":[{"value":' + number + '}]}').encode())
                    with self.assertRaisesRegex(ValueError, 'Non-finite JSON number'):
                        self.install()
            self.replace_payload(name, b'{}')


if __name__ == '__main__': unittest.main()
