# Changelog

## 0.6.0
- Time of day in exported demos: *Custom* mode with an animatable **Hour**; recorded time shown in the panel.
- CLI `demotime` (fixed time or ramp).
- Rust folder found automatically from Steam libraries (newest of Rust / RustStaging).

## 0.5
- Demo export from Blender: moved/animated entities, Open/On/Locked keys, deletions, clones, grade changes, crop to load boxes.
- Lossless demo read/write (`demoroundtrip`).
- Hinged doors shown open/closed and animated from the `Open` property; flag history imported from the recording.

## 0.4
- Buildings: grade prefabs and wall parts (ConditionalModel); demo import takes all entities by default.

## 0.3
- Sidebar panel: load assets for the whole map, selection, load boxes, cursor radius, camera path or a checklist.
- Detail levels: LOD0, far LOD, monuments as HLOD; textures optional; background extraction.
- Extractor built into the add-on.

## 0.2
- Asset extraction from the game bundles (meshes, materials, textures) and loading into prefab collections.
- Prefab names resolved from the game (MD5 of the asset path).

## 0.1
- Map import (terrain, splat colours, water, paths, prefab placeholders) and demo import (entities with motion, players, own ticks).
