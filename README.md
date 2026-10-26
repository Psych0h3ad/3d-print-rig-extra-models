Additional machine and Layout reference models for [3D Print Rig](https://github.com/Psych0h3ad/3d-print-rig).

Each model retains its author, pinned source revision and individual license in the asset metadata. Annex K1/K2/K3 retain the Annex Engineering Hardware and Software EULA and original native reference issues; these are static assembly previews. Remorph is by Lex Reman under CERN-OHL-W-2.0; its supplemental Rapido integration component retains separate manufacturer terms.

Layout exterior references retain their [supplied notices](licenses/U1_exterior_notices.json).
The U1 fixed plate's [source record](licenses/U1_BED_SOURCE_RECEIPT.json) identifies
its upstream contour and revision. The original [AGPL notice](licenses/Snapmaker_OrcaSlicer_LICENSE.txt)
is available in full. These references are intended for room planning.

Additional Layout reference versions are listed in `site/LAYOUT_REFERENCE_SET.json`.
Each version has a directory derived from its exact file identities. The builder
checks the complete directory, file sizes, hashes and model containers before
publishing it alongside the original bundles, keeping existing model URLs available.

The builder always includes the original and ELEGOO bundles. When
`site/LAYOUT_ASSET_BUNDLE.json` is present, it also requires the layout equipment
bundle. An invalid pin or unavailable bundle fails the build. The layout pin
selects the four exterior reference transfers and shared preview data.

The optional ZIP contains `VIEWER_ASSETS.json` and exactly these six assets:

- `layout/assets/equipment/u1-exterior/U1_exterior_standalone_loaded.glb.gz`
- `layout/assets/equipment/u1-exterior/U1_exterior_docked.glb.gz`
- `layout/assets/equipment/u1-exterior/PopStationMini_empty.glb.gz`
- `layout/assets/equipment/u1-exterior/PopStationMini_loaded.glb.gz`
- `layout/assets/equipment/u1-exterior/U1_preview_geometry.json.gz`
- `layout/assets/equipment/u1-exterior/NOTICE.txt`

The layout pin has exactly `version`, `url`, `bytes`, `sha256`, `files`,
`site_bytes`, `manifest_bytes`, and `manifest_sha256`. Version is 1 and files is
6. Byte counts and SHA256 values cover the exact ZIP, exact manifest, and each
asset. The URL must identify a ZIP in this repository's GitHub releases; an
offline test pin may use null. No alternative download is attempted.

The manifest has exactly `version` (1) and `files`. Each record has exactly
`path`, `bytes`, and `sha256`; records are sorted by path. Canonical JSON uses
sorted object keys, ASCII escaping, compact separators, and one final LF.
The ZIP uses stored entries because the models are already gzip compressed.
Directories, links, encryption, comments, extra ZIP fields, unexpected paths,
duplicate records, and case-insensitive or file/directory collisions are rejected.

The combined site cap remains 900,000,000 bytes, including the published pins.
The optional equipment assets are limited to 64,000,000 bytes in total, the ZIP
to 65,000,000 bytes, each model to 16 MiB (16,777,216 bytes) both gzip encoded
and expanded, and the manifest to 65,536 bytes. The preview geometry
dictionary is UTF-8 JSON, limited to 16 MiB (16,777,216 bytes) both gzip encoded
and expanded. It is parsed as data; duplicate keys and non-finite numbers are
rejected. NOTICE is nonempty UTF-8 plain text of at most 4,096 bytes, with LF
line endings. Public HTTPS license and source links and copyright text are
allowed; local drive, UNC, file URI paths and control characters are rejected.
It is included verbatim and requires wording review. Model gzip checks and GLB header/length checks do not
establish geometry or mounting clearance.

Build into a new directory with `python scripts/build_site.py --output _site`.

Source assemblies can also provide `site/SOURCE_CHUNKS.json`. It pins a versioned
archive containing exact geometry chunks, the source metadata, and author/license
notices. The builder verifies the archive and each file, checks decoded gzip
identities, and keeps existing model URLs available. The combined site size limit
still applies. Use `--source-assets source-chunks.zip` for an offline build.
Run its format and integrity checks with `python scripts/test_source_chunks.py`.

For an offline build, supply `--assets original.zip --elegoo-assets elegoo.zip`
and, when pinned, `--layout-assets equipment.zip`. Supplying a layout ZIP without
its pin is an error. The current Pages build command needs no change.

Run layout delivery fixtures with
`python scripts/test_layout_bundle.py --work-dir test-work --report layout-tests.json`.
Add `--assets original.zip --elegoo-assets elegoo.zip` to also verify every
existing exported byte in both two-bundle and three-bundle builds. Layout tests
use a small triangle fixture; it is never a released exterior model.
