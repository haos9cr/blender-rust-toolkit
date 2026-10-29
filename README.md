# Rust World for Blender

[Русская версия](README.ru.md)

A Blender add-on for working with **Rust** (Facepunch) worlds outside the game:

- **import** procedural/custom maps (`.map`) — terrain, water, roads, monuments and decor;
- **import** demos (`.dem`) — every entity of the recording (buildings, doors, deployables, trees, players, NPCs, vehicles) with its movement over time;
- **load the real game assets** (meshes, materials, textures) straight from your Rust install, for the whole map or only where you need them;
- **edit the scene and export a new demo** — move or animate entities, open/close doors, delete or add entities, change building grades, set the time of day.

Made for cinematic camera work: build the shot in Blender against the real geometry, then play it back in the game.

> Single file, no game data included. Assets are read from your own Rust installation.

---

## Requirements

- Blender **4.2+** (developed and tested on 5.2).
- Rust installed through Steam (release or staging branch).
- Internet once, to install the asset extractor's Python packages (UnityPy, Pillow) from the add-on panel.

## Installation

1. Download `rust_world.py`.
2. Blender › *Edit › Preferences › Add-ons › Install from Disk…* › pick the file, enable **Rust world (.map / .dem)**.
3. In the add-on preferences check **Rust folder** (found automatically from your Steam libraries; set it by hand if needed). **Asset cache** is optional (default: `<Rust folder>/rust_blender_cache`).
4. Open the sidebar in the 3D view (**N**) › tab **Rust** › **Install extractor dependencies** (one time).

## Quick start

1. *File › Import › Rust map (.map)* — maps are in `<Rust>/maps` (the server's map is downloaded there when you join).
2. *File › Import › Rust demo (.dem)* — demos are in `<Rust>/demos`.
3. Sidebar › **Rust** › choose what to load and press **Load assets**.
   Extraction runs in the background with a progress bar; what is already cached is never extracted again.
4. Edit, then *File › Export › Rust demo (.dem)* — the result is a new file next to the recording.

## Loading assets

**Load** — what to load:

| Option | Loads |
|---|---|
| Whole map | every placed prefab |
| Selected | prefab instances selected in the viewport |
| Inside boxes | instances inside any load box (**Add load box** puts a box at the 3D cursor; move/scale/rotate it, use as many as you like) |
| Around cursor | within a radius of the 3D cursor |
| Along camera path | within a radius of the scene camera over the frame range |
| From list | prefabs checked in **Prefab list** (search, check by category, from viewport selection, select instances) |

**Detail**

- *LOD0* — full detail.
- *Far LOD* — the farthest LOD that has a mesh; silhouettes only, very light.
- *Monuments as one HLOD mesh* — each monument as a single merged far-distance mesh.
- *Textures* — off by default (meshes + material colours are much faster); can be added later, cached meshes are reused.
- *Hide instances outside* — hide everything outside the chosen scope.

For reference, a 2 km map: whole map in *Far LOD + HLOD* ≈ 0.45 M polygons, loads in ~10 s; full *LOD0* ≈ 9 M polygons.

**Buildings** get the mesh of their real grade (twig / wood / stone / metal / armored and skins) and wall parts (corners, halves). **Hinged doors** are shown open or closed and swing with the `Open` property.

## Editing and exporting a demo

The exported demo is the original recording plus your edits. **Everything you did not touch is copied byte for byte** and the recording is never overwritten.

Panel **Rust › Demo edit / export**:

- **Key Open / Key On / Key Lock** — toggle a flag on the selected entities at the current frame (doors, boxes, lights, furnaces…).
- **Clone entity** (or plain *Shift+D*) — a new entity copied from an existing one: another door, wall, box…
- **Grade** of a building block (0 twig … 4 armored), **Grades** refreshes the look.
- **Time of day** — *As recorded* or *Custom* with an animatable **Hour**; the panel shows the recorded time at the current frame.

Export options:

| Option | What it does |
|---|---|
| Recording | which imported demo is the base |
| Moved / animated entities | entities you moved or animated (also through an animated parent, e.g. an empty at a door hinge) get your motion |
| Open / On / Locked keys | flag keys are written; untouched flags stay as recorded |
| Deleted entities | entities deleted in Blender (and their children) are removed |
| New (cloned) entities | clones are added as new entities |
| Keep only inside load boxes | everything outside the boxes is cut; your own player is always kept |
| Position rate (Hz) | how often animated positions are written (30 by default) |

Frame 1 of the scene matches the same demo moment as on import, so a camera made for the demo lines up with your edits in time.

## Command line (no Blender)

```
python rust_world.py mapinfo   file.map
python rust_world.py demoinfo  file.dem
python rust_world.py demojson  file.dem out.json
python rust_world.py prefabnames <Rust folder> names.json
python rust_world.py extract --rust <Rust folder> --map file.map --dem file.dem [--lod lod0|low] [--hlod] [--no-tex] [--max-tex 1024]
python rust_world.py demoroundtrip file.dem                 # read + write, must be byte-identical
python rust_world.py demotime in.dem out.dem 17:30           # fixed time of day
python rust_world.py demotime in.dem out.dem 6:00-20:00 30   # 06:00 -> 20:00 over 30 s
```

## How it works

The file formats were reverse-engineered; notes are in [docs/FORMATS.md](docs/FORMATS.md). In short: maps and demos are protobuf (maps LZ4-compressed), prefab IDs are a hash of the asset path, and the game bundles are stored uncompressed, so the extractor reads them lazily through memory mapping (a few hundred MB of RAM for ~50 GB of bundles) with UnityPy.

## Known limitations

- Players and NPCs are shown as simple stand-ins (their clothing is assembled at runtime).
- Foundation and roof parts use a sensible default set; only wall parts follow the recording exactly.
- Only single hinged doors swing in Blender; double doors, gates and garage doors don't yet.
- Some shaders are simplified (blend layers, terrain-blend details); terrain uses splat colours, not the game's terrain textures.
- Demos depend on the game version: after a monthly update old (and generated) demos may stop playing.

## Disclaimer

Not affiliated with or endorsed by Facepunch Studios. No game files are distributed with this project; the add-on reads assets from your own installation. Use extracted assets in accordance with the Rust EULA and Facepunch's terms.

## License

[MIT](LICENSE)
