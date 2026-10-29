"""Exact mesh-only BIQU sidecar. Old reference directories remain untouched."""
import gzip
import hashlib
import json
import os
from pathlib import Path

PREFIX = 'layout-references/biqu-popstation-0759dac8fdee8cf20c6c/layout/assets/equipment/biqu-popstation-cad-v1'
FILES = (
    ('NOTICE.txt', 943, 'b434c5ff93adf1a55e465b5a3ac379bc0fa3bca3897c99a648f7d588887cf49c'),
    ('PopStationMini_preview_geometry.json', 9549487, 'f7ae02fccb83c655c4784af5736eec59fb4064ca4e5f714846f7b88f34f3ab6c'),
    ('PopStationMini_preview_geometry.json.gz', 1451459, '93f69bed29fb42022d3bd5535830c8579b778c63017206308890552793346ca2'),
    ('PopStationMini_source_empty.glb', 1347856, 'cd531f04bf1db39af6826aa658fe30a2e9efaafd1209e5d61ff4548c4970c83b'),
    ('PopStationMini_source_empty.glb.gz', 437640, 'e3125f9042d4dafdb33534709c7098acac8f0c634f6a95f1ff284e5b79b7da75'),
)


def no_links(path):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('BIQU reference links forbidden')


def install(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    root = source / PREFIX
    # An offline legacy fixture has no BIQU sidecar. An actual checkout always
    # carries all five files; partial directories and extra/private files fail.
    if not os.path.lexists(root):
        if source == Path(__file__).resolve().parents[1] / 'site':
            raise ValueError('Missing BIQU actual reference directory')
        return 0
    for parent in [root, *root.parents]:
        no_links(parent)
        if parent == source:
            break
    if set(p.name for p in root.iterdir()) != {name for name, _, _ in FILES}:
        raise ValueError('BIQU mesh-only inventory differs')
    bodies = {}
    for name, size, digest in FILES:
        file = root / name
        no_links(file)
        if not file.is_file() or file.stat().st_size != size:
            raise ValueError('BIQU asset size differs')
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('BIQU asset hash differs')
        bodies[name] = data
    for name in ('PopStationMini_source_empty.glb', 'PopStationMini_preview_geometry.json'):
        # Identity verified before inflation; exact small source caps above.
        if gzip.decompress(bodies[name + '.gz']) != bodies[name]:
            raise ValueError('BIQU encoded/decoded identity differs')
    preview = json.loads(bodies['PopStationMini_preview_geometry.json'])
    if preview['schema'] != 'biqu-popstation-preview-v1' or len(preview['sourceLeaves']) != 70:
        raise ValueError('BIQU preview correspondence differs')
    destination = target / PREFIX
    if not destination.resolve().is_relative_to(target) or os.path.lexists(destination):
        raise ValueError('BIQU destination collision')
    for parent in [destination, *destination.parents]:
        no_links(parent)
        if parent == target:
            break
    destination.mkdir(parents=True)
    for name, data in bodies.items():
        with (destination / name).open('xb') as stream:
            stream.write(data)
    return len(FILES)
