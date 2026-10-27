# E3NG v1.2 cooling options

RH3D's Revo-S dual-5015 duct, complete 5015 fans, six M3×20 screws,
two M3×35 screws and four M3 inserts, alongside Synthead's reinforced base
and cable-hole guide. Used by [3D Print Rig](https://github.com/Psych0h3ad/3d-print-rig).

[Sources, exact revisions and hashes](sources.json) identify each component.
[NOTICE.txt](NOTICE.txt) lists the authors and separate component licenses.
The original two Synthead meshes retain CC BY-NC-SA 4.0. RH3D and Archetype
components retain CC BY-NC-SA 4.0; SnakeOilXY screws retain GPL-3.0.
The collection does not relicense these components under a single license.

Four sacrificial printing-support bodies are omitted from the duct. Its
functional triangles remain unchanged. Source geometry is shared between
identical instances; rigid node transforms place each part. Screw meshes
retain the source's simplified threads, including one collapsed tessellation
triangle with a same-face unit normal. No rendered source surface is removed.

The `sources/` directory contains the original selected purchased STEP parts
and SnakeOilXY's editable FreeCAD assembly. Corresponding license texts are in
`licenses/`. Native source geometry and units are unchanged; only tessellation
and rigid placement are applied. The public E3NG/VIRTU insert reference and
the printed-part originals remain linked in the source index.

This is a mechanical display reference. Selected native feature and exported
mesh checks do not certify outlet sealing, insert/thread engagement, complete
physical fit or continuous travel. Follow RH3D's build guide for a real machine.
