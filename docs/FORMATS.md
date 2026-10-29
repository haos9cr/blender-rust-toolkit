# File format notes

Reverse-engineered while building the add-on; verified against real files from the game (2026, Unity 6000.3).
Everything here is what the code relies on — see `rust_world.py` for the details.

## Coordinates

- Unity: X right, **Y up**, Z forward, left-handed. Blender: Z up, right-handed.
- Position: Unity `(x, y, z)` → Blender `(x, z, y)`.
- Object transform: `M_blender = S · M_unity · S`, where `S` swaps Y and Z (the swap mirrors, so triangle winding is reversed for meshes).
- Unity Euler angles (`Quaternion.Euler(x, y, z)`) rotate Z, then X, then Y: `R = Ry · Rx · Rz`.
- A rotation of `+a` about Unity Y is `−a` about Blender Z.

## Prefab IDs

`prefabID = first 4 bytes (little-endian) of MD5(asset path in lower case)`.
Asset paths come from `Bundles/AssetSceneManifest.json` (scenes → included assets).
GUID → path (for `GameObjectRef` fields) comes from `assets/manifest.asset` (`GameManifest`: `prefabProperties`, `guidPaths`) in `shared/content.bundle`.

## .map (WorldSerialization, version 10)

```
uint32  version (10)
int64   timestamp
...     LZ4 stream (lz4net LZ4Stream): repeated { varint flags, varint original size,
        [varint compressed size if flags & 1], block }  ->  protobuf WorldData
```

WorldData:

| Field | Content |
|---|---|
| 1 | size in metres |
| 2 | maps `{1 name, 2 data}` |
| 3 | prefabs `{1 category, 2 prefab id, 3 pos, 4 euler (deg), 5 scale}` |
| 4 | paths `{1 name, 5 width, …, 15 nodes[vec3]}` (roads, rivers, power lines) |

Map layers: `height`, `terrain`, `water` — `res × res` uint16, height = `v / 32766 · 1000 − 500` m; grid index `[z][x]`, world x = `−size/2 + ix/(res−1)·size`.
`splat` 8 × 1024² bytes (Dirt, Snow, Sand, Rock, Grass, Forest, Stones, Gravel), `biome` 5 × 1024², `topology` 1024² int32 bit mask, `alpha` 1024².

## .dem

```
byte    n, "RUST DEMO FORMAT"
int32   header length, protobuf header
byte    0
packets: { int32 length, uint64 time (ms), data[length], 2 bytes 00 00 }
```

Header: 2 level name, 3 seed, 4 size, 5 checksum, 6 SteamID of the recording player, 9 map URL, 11 length (ms).

`data[0]` is the message type (+140). For every entity message the entity uid (uint64) follows the type byte, except `Entities`.

| Type | Message | Layout |
|---|---|---|
| 145 | Entities | `[u32][protobuf Entity]` — full snapshot |
| 146 | EntityDestroy | `[u64 uid][u8 mode]` |
| 147 | GroupChange | `[u64 uid][u32 group]…` |
| 149 | RPC | `[u64 uid]…` |
| 150 | EntityPosition | `[u64 uid][vec3 pos][vec3 euler][f32 server time]` (37 bytes) |
| 155 | Tick (own player) | `PlayerTick {1 input {2 aim angles}, 2 position}` |
| 163 | EntityFlags | `[u64 uid][i32 flags]` |

Entity protobuf (the parts used):

| Field | Content |
|---|---|
| 1 | baseNetworkable `{1 uid, 2 group, 3 prefabID}` |
| 2 | baseEntity `{1 pos, 2 euler, 3 flags, 4 time}` |
| 3 | basePlayer `{1 name, 2 SteamID}` |
| 6 | buildingBlock `{1 model (ConditionalModel bit mask), 2 grade 0..4}` |
| 7 | environment `{1 dateTime (.NET DateTime.ToBinary), 6 engine time (s)}` — entity `net_env.prefab` |
| 10 | parent `{1 uid, 2 bone}` — child pose is local to the parent |

Entity flags: `On = 2`, `Open = 8`, `Locked = 16`.

Findings verified in game:

- The client applies `EntityPosition` to any entity (also ones that never move, like doors), so arbitrary animation works.
- An `EntityFlags` message alone opens/closes a door with the game's own animation.
- Rewriting `dateTime` in the environment snapshots changes the time of day; extra snapshots every 0.5 s give a smooth transition.
- A demo can be read and written back byte-identical; untouched packets are kept verbatim.

## Buildings

- A block prefab (e.g. `foundation.prefab`) holds only a collision mesh and `ConstructionGrade` components: `gradeBase` → BuildingGrade (`type` 0 twig … 4 armored, `skin`), `skinObject` → grade prefab (`foundation.metal.prefab`).
- Grade prefabs (walls, roofs) are assembled from `ConditionalModel` parts (`wall.metal.full`, `…left`, `…corner.l` …).
  For walls, bit *i* of the block's `model` field enables part *i* in `GetComponentsInChildren(true)` order. For foundations and roofs the server list also contains server-only parts stripped from the client bundles, so the bits don't line up.

## Doors

Single hinged doors (`door.hinged.*`) open **+90° about Unity Y** around the pivot of the leaf node (≈ `(0.02, 1.17, −0.6)` in entity space); the handle is a separate node that turns with the leaf.

## Bundles

All bundles are UnityFS (format 8) with uncompressed data blocks (only the block info is LZ4HC), so each CAB / `.resS` is a plain byte range of the bundle file and can be memory-mapped.
Prefabs live in AssetScenes (`shared/assetscenes.bundle`, monuments in `monument.N` scenes); roots are named by full lower-case asset path and are inactive templates.

LOD mechanisms seen:

- children named `*_LOD0 … _LODn` (the last ones are often empty);
- `RendererLOD` / `TreeLOD` — one renderer per distance;
- `MeshLOD` — one mesh per distance on the same renderer;
- `LODMasterMesh` — monument HLOD, `ReplacementMesh` points to a renderer;
- `BiomeVisuals` — per-biome variants (`_arid`, `_arctic`, …).

Trees: the spawned `TreeEntity` prefab has no visuals; `TreeEntity.prefab` (GameObjectRef) points to the visual prefab.
