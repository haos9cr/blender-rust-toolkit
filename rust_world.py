"""
Rust world <-> Blender: import procedural/custom maps (.map), demos (.dem) and game assets,
edit the scene and export a new demo. See README.md.

.map  (WorldSerialization, version 10):
  uint32 version, int64 timestamp, then a chunked LZ4 stream (lz4net LZ4Stream:
  varint flags, varint original size, [varint compressed size], block) -> protobuf WorldData
    1 size (metres)
    2 maps    {1 name, 2 data}   terrain/height/water: (res x res) uint16, value/32766*1000-500 = height
                                 splat: 8 x 1024^2 bytes (Dirt Snow Sand Rock Grass Forest Stones Gravel)
                                 biome: 5 x 1024^2, topology: 1024^2 int32 bitmask, alpha: 1024^2
    3 prefabs {1 category, 2 prefab id, 3 pos, 4 euler deg, 5 scale}
    4 paths   {1 name, 5 width, ..., 15 nodes[vec3]}
  Grid index is [z][x]; world x = -size/2 + ix/(res-1)*size.

.dem:
  string "RUST DEMO FORMAT", int32 len + protobuf header
    (2 level, 3 seed, 4 size, 6 recorder steamid, 9 map url, 11 length ms),
  then packets: [u16 0][int32 len][uint64 time ms][data]   (first packet: 1 separator byte)
  data[0] = message type + 140:
    145 Entities        [u32 num][Entity: 1 {1 uid, 2 group, 3 prefabID}, 2 {1 pos, 2 euler, 3 flags, 4 time},
                                         3 basePlayer {1 name, 2 userid}, 10 parent {1 uid, 2 bone}]
    146 EntityDestroy   [u64 uid][u8 mode]
    150 EntityPosition  [u64 uid][vec3 pos][vec3 euler][float server time]
    155 Tick (own)      PlayerTick {1 input {2 aim angles}, 2 position}

CLI (no Blender):
  python rust_world.py mapinfo  file.map
  python rust_world.py demoinfo file.dem
  python rust_world.py demojson file.dem out.json
  python rust_world.py prefabnames <Rust folder | AssetSceneManifest.json> rust_prefab_names.json

Prefab ids (map + demo) = first 4 bytes LE of MD5(lower-case asset path); paths come from
Bundles/AssetSceneManifest.json, so names are resolved straight from the game folder
(add-on preferences: "Rust folder").

Blender: Edit > Preferences > Add-ons > Install > this file.
  File > Import > Rust map (.map)
  File > Import > Rust demo (.dem)
  File > Import > Rust prefab assets (cache)   <- meshes/materials extracted from the game bundles
  File > Export > Rust demo (.dem)
Prefabs are placed as collection instances: one library collection per prefab id
("RustPrefab_<id>"), so replacing the placeholder in that collection updates every
instance (map and demo share them). Optional names: rust_prefab_names.json {id: path}
next to the .map/.dem or this add-on.
"""

import json
import math
import os
import re
import struct
import sys

bl_info = {
    "name": "Rust world (.map / .dem)",
    "author": "haos9cr",
    "version": (0, 6, 0),
    "blender": (4, 2, 0),
    "location": "File > Import / Export, 3D View > Sidebar > Rust",
    "description": "Import Rust maps, demos and assets; edit and export demos",
    "category": "Import-Export",
}

# ---------------------------------------------------------------- protobuf / lz4 low level


def _read_varint(b, i):
    r = 0
    s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b):
    i = 0
    n = len(b)
    while i < n:
        key, i = _read_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _read_varint(b, i)
        elif w == 1:
            v = struct.unpack_from("<d", b, i)[0]
            i += 8
        elif w == 5:
            v = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif w == 2:
            ln, i = _read_varint(b, i)
            v = b[i:i + ln]
            i += ln
        else:
            raise ValueError("wire type %d" % w)
        yield f, w, v


def _kv(b):
    return {f: v for f, w, v in _fields(b)}


def _vec(b):
    if not b:
        return (0.0, 0.0, 0.0)
    m = _kv(b)
    return (m.get(1, 0.0), m.get(2, 0.0), m.get(3, 0.0))


def lz4_block_decompress(src, out_size):
    dst = bytearray(out_size)
    si = di = 0
    n = len(src)
    while si < n:
        tok = src[si]
        si += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                c = src[si]
                si += 1
                lit += c
                if c != 255:
                    break
        if lit:
            dst[di:di + lit] = src[si:si + lit]
            si += lit
            di += lit
        if si >= n:
            break
        off = src[si] | (src[si + 1] << 8)
        si += 2
        ml = tok & 15
        if ml == 15:
            while True:
                c = src[si]
                si += 1
                ml += c
                if c != 255:
                    break
        ml += 4
        s = di - off
        if off >= ml:
            dst[di:di + ml] = dst[s:s + ml]
        else:
            pat = bytes(dst[s:di])
            dst[di:di + ml] = (pat * (ml // off + 1))[:ml]
        di += ml
    return bytes(dst[:di])


try:
    import lz4.block as _lz4
except ImportError:
    _lz4 = None


def lz4stream_decompress(b, i=0):
    out = bytearray()
    while i < len(b):
        flags, i = _read_varint(b, i)
        orig, i = _read_varint(b, i)
        if flags & 1:
            clen, i = _read_varint(b, i)
            blk = b[i:i + clen]
            out += _lz4.decompress(blk, uncompressed_size=orig) if _lz4 else lz4_block_decompress(blk, orig)
            i += clen
        else:
            out += b[i:i + orig]
            i += orig
    return bytes(out)


# ---------------------------------------------------------------- map


def read_map(path):
    with open(path, "rb") as fh:
        b = fh.read()
    version = struct.unpack_from("<I", b, 0)[0]
    if version < 10:
        raise ValueError("map version %d not supported (expected 10)" % version)
    timestamp = struct.unpack_from("<q", b, 4)[0]
    data = lz4stream_decompress(b, 12)
    world = {"version": version, "timestamp": timestamp, "size": 0, "maps": {}, "prefabs": [], "paths": []}
    for f, w, v in _fields(data):
        if f == 1:
            world["size"] = v
        elif f == 2:
            m = _kv(v)
            world["maps"][m[1].decode()] = m.get(2, b"")
        elif f == 3:
            m = _kv(v)
            world["prefabs"].append({
                "category": m.get(1, b"").decode(), "id": m.get(2, 0),
                "pos": _vec(m.get(3)), "rot": _vec(m.get(4)),
                "scale": _vec(m.get(5)) if 5 in m else (1.0, 1.0, 1.0)})
        elif f == 4:
            p = {"name": "", "width": 0.0, "nodes": []}
            for pf, pw, pv in _fields(v):
                if pf == 1:
                    p["name"] = pv.decode()
                elif pf == 5:
                    p["width"] = pv
                elif pf == 15:
                    p["nodes"].append(_vec(pv))
            world["paths"].append(p)
    return world


def short_res(world, name):
    n = len(world["maps"][name]) // 2
    return int(round(math.sqrt(n)))


# ---------------------------------------------------------------- demo

MSG_ENTITIES, MSG_DESTROY, MSG_POSITION, MSG_TICK = 145, 146, 150, 155


def read_demo(path):
    with open(path, "rb") as fh:
        b = fh.read()
    n = b[0]
    if b[1:1 + n] != b"RUST DEMO FORMAT":
        raise ValueError("not a Rust demo")
    i = 1 + n
    hl = struct.unpack_from("<i", b, i)[0]
    h = _kv(b[i + 4:i + 4 + hl])
    i += 4 + hl
    header = {"level": h.get(2, b"").decode(), "seed": h.get(3, 0), "size": h.get(4, 0),
              "recorder": h.get(6, 0), "url": h.get(9, b"").decode(), "length_ms": h.get(11, 0)}
    if b[i] == 0:  # separator before the first packet is one zero byte, later ones are two
        i += 1
    ents = {}
    ticks = []
    while i + 12 <= len(b):
        ln, t = struct.unpack_from("<iQ", b, i)
        d = b[i + 12:i + 12 + ln]
        i += 12 + ln + 2
        if not d:
            continue
        typ = d[0]
        ts = t / 1000.0
        if typ == MSG_ENTITIES:
            fl = {}
            for f, w, v in _fields(d[5:]):
                fl.setdefault(f, v)
            bn = _kv(fl.get(1, b""))
            uid = bn.get(1)
            if uid is None:
                continue
            e = ents.get(uid)
            if e is None:
                e = ents[uid] = {"uid": uid, "prefab": bn.get(3, 0), "group": bn.get(2, 0),
                                 "spawn": ts, "destroy": None, "parent": 0, "bone": 0,
                                 "name": "", "userid": 0, "keys": [], "flag_hist": []}
            e["destroy"] = None  # re-sent: alive again
            if 2 in fl:
                be = _kv(fl[2])
                e["keys"].append([ts, list(_vec(be.get(1))), list(_vec(be.get(2)))])
                e["flags"] = be.get(3, 0)
                if not e["flag_hist"] or e["flag_hist"][-1][1] != e["flags"]:
                    e["flag_hist"].append((ts, e["flags"]))
            if 3 in fl:
                bp = _kv(fl[3])
                e["name"] = bp.get(1, b"").decode("utf-8", "replace")
                e["userid"] = bp.get(2, 0)
            if 6 in fl:  # buildingBlock {1 model (conditional parts), 2 grade (0 twig .. 4 armored)}
                try:
                    bb = _kv(fl[6])
                    if 2 in bb or 1 in bb:
                        e["grade"] = bb.get(2, 0)
                        e["model"] = bb.get(1, 0)
                except Exception:
                    pass
            if 10 in fl:
                pi = _kv(fl[10])
                e["parent"] = pi.get(1, 0)
                e["bone"] = pi.get(2, 0)
        elif typ == MSG_POSITION:
            for o in range(1, len(d) - 35, 36):
                uid = struct.unpack_from("<Q", d, o)[0]
                v = struct.unpack_from("<6f", d, o + 8)
                e = ents.get(uid)
                if e is not None:
                    e["keys"].append([ts, list(v[:3]), list(v[3:])])
        elif typ == MSG_FLAGS and len(d) >= 13:
            uid, fl = struct.unpack_from("<Qi", d, 1)
            e = ents.get(uid)
            if e is not None:
                e["flags"] = fl
                if not e["flag_hist"] or e["flag_hist"][-1][1] != fl:
                    e["flag_hist"].append((ts, fl))
        elif typ == MSG_DESTROY:
            uid = struct.unpack_from("<Q", d, 1)[0]
            if uid in ents:
                ents[uid]["destroy"] = ts
        elif typ == MSG_TICK:
            try:
                pt = _kv(d[1:])
                inp = _kv(pt.get(1, b""))
                ticks.append([ts, list(_vec(pt.get(2))), list(_vec(inp.get(2)))])
            except Exception:
                pass
    for e in ents.values():  # collapse duplicate keys
        k = e["keys"]
        e["keys"] = [a for j, a in enumerate(k) if j == 0 or a[1:] != k[j - 1][1:]]
    return {"header": header, "entities": list(ents.values()), "ticks": ticks}


# ---------------------------------------------------------------- demo writing (lossless)
# A demo is kept as raw packets: unknown messages are copied byte for byte, only what we add/change is new.


def read_demo_raw(path):
    with open(path, "rb") as fh:
        b = fh.read()
    n = b[0]
    i = 1 + n
    hl = struct.unpack_from("<i", b, i)[0]
    i += 4 + hl
    if b[i] == 0:
        i += 1
    head = b[:i]
    packets = []
    while i + 12 <= len(b):
        ln, t = struct.unpack_from("<iQ", b, i)
        if ln < 0 or i + 12 + ln > len(b):
            break
        d = b[i + 12:i + 12 + ln]
        i += 12 + ln
        trail = b[i:i + 2]
        i += len(trail)
        packets.append([t, d, trail])
    return {"head": head, "packets": packets, "tail": b[i:]}


def demo_bytes(raw):
    out = bytearray(raw["head"])
    for t, d, trail in raw["packets"]:
        out += struct.pack("<iQ", len(d), t) + d + trail
    out += raw.get("tail", b"")
    return bytes(out)


def write_demo_raw(raw, path):
    out = demo_bytes(raw)
    with open(path, "wb") as fh:
        fh.write(out)
    return len(out)


def demo_insert(raw, new_packets):
    """Merge [(t_ms, data)] into the stream, keeping time order (new ones go after existing ones at equal t)."""
    import bisect
    pk = raw["packets"]
    times = [p[0] for p in pk]
    for t, d in sorted(new_packets, key=lambda x: x[0]):
        k = bisect.bisect_right(times, t)
        pk.insert(k, [int(t), d, b"\0\0"])
        times.insert(k, int(t))
    return raw


def demo_server_clock(raw):
    """Linear map demo ms -> server time used in EntityPosition packets."""
    xs, ys = [], []
    for t, d, _ in raw["packets"]:
        if d and d[0] == MSG_POSITION and len(d) >= 37:
            xs.append(t / 1000.0)
            ys.append(struct.unpack_from("<f", d, 33)[0])
    if not xs:
        return lambda ms: ms / 1000.0
    off = sorted(y - x for x, y in zip(xs, ys))[len(xs) // 2]  # median offset, slope 1
    return lambda ms: ms / 1000.0 + off


MSG_FLAGS = 163


def pk_entity_position(uid, pos, euler, server_time):
    return bytes([MSG_POSITION]) + struct.pack("<Q6ff", uid, *pos, *euler, server_time)


def pk_entity_flags(uid, flags):
    return bytes([MSG_FLAGS]) + struct.pack("<Qi", uid, flags)


FLAG_OPEN = 8
FLAG_ON = 2
DOOR_OPEN_DEG = 90.0  # hinged doors swing +90 deg about the hinge (Unity Y), checked against an in-game shot


def is_door_path(path):
    return "door.hinged" in path and "cargo_ship" not in path
FLAG_LOCKED = 16
ENTITY_MSGS = (MSG_ENTITIES, MSG_DESTROY, 147, 149, MSG_POSITION, MSG_FLAGS)  # uid right after the type byte


def _pb_parse(b):
    """Top-level protobuf fields, values kept raw: [field, wire, int | bytes]."""
    out, i, n = [], 0, len(b)
    while i < n:
        key, i = _read_varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _read_varint(b, i)
        elif w == 1:
            v, i = bytes(b[i:i + 8]), i + 8
        elif w == 5:
            v, i = bytes(b[i:i + 4]), i + 4
        elif w == 2:
            ln, i = _read_varint(b, i)
            v, i = bytes(b[i:i + ln]), i + ln
        else:
            raise ValueError("wire type %d" % w)
        out.append([f, w, v])
    return out


def _varint_enc(v):
    if v < 0:
        v &= (1 << 64) - 1
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def _pb_encode(fields):
    out = bytearray()
    for f, w, v in fields:
        out += _varint_enc((f << 3) | w)
        if w == 0:
            out += _varint_enc(v)
        elif w == 2:
            out += _varint_enc(len(v)) + v
        else:
            out += v
    return bytes(out)


def _pb_vec(v):
    return _pb_encode([[1, 5, struct.pack("<f", v[0])], [2, 5, struct.pack("<f", v[1])], [3, 5, struct.pack("<f", v[2])]])


def _pb_set(fields, f, w, v):
    for it in fields:
        if it[0] == f:
            it[1], it[2] = w, v
            return
    fields.append([f, w, v])


def entity_uid(d):
    """uid of an entity message (None for others)."""
    if not d or d[0] not in ENTITY_MSGS:
        return None
    if d[0] == MSG_ENTITIES:
        for f, w, v in _fields(d[5:]):
            if f == 1:
                return _kv(v).get(1)
        return None
    return struct.unpack_from("<Q", d, 1)[0] if len(d) >= 9 else None


def entity_rewrite(d, uid=None, group=None, pos=None, euler=None, flags=None, grade=None, drop_parent=False):
    """Entities packet with some fields replaced; everything else stays byte-identical."""
    top = _pb_parse(d[5:])
    for it in top:
        if it[0] == 1 and (uid is not None or group is not None):
            sub = _pb_parse(it[2])
            if uid is not None:
                _pb_set(sub, 1, 0, uid)
            if group is not None:
                _pb_set(sub, 2, 0, group)
            it[2] = _pb_encode(sub)
        elif it[0] == 2 and (pos is not None or euler is not None or flags is not None):
            sub = _pb_parse(it[2])
            if pos is not None:
                _pb_set(sub, 1, 2, _pb_vec(pos))
            if euler is not None:
                _pb_set(sub, 2, 2, _pb_vec(euler))
            if flags is not None:
                _pb_set(sub, 3, 0, int(flags))
            it[2] = _pb_encode(sub)
        elif it[0] == 6 and grade is not None:
            sub = _pb_parse(it[2])
            _pb_set(sub, 2, 0, int(grade))
            it[2] = _pb_encode(sub)
    if drop_parent:
        top = [it for it in top if it[0] != 10]
    return d[:5] + _pb_encode(top)


# ---- time of day: net_env.prefab entity, Entity field 7 {1 dateTime (.NET DateTime.ToBinary), 6 engine time s}
_TICKS_MASK = (1 << 62) - 1
_TICKS_PER_DAY = 864000000000


def env_hours(binary):
    """.NET DateTime.ToBinary -> hour of day (float)."""
    return ((binary & _TICKS_MASK) % _TICKS_PER_DAY) / 36000000000.0


def env_with_hours(binary, hours):
    """Same date (and DateTimeKind bits), new time of day."""
    kind = binary & ~_TICKS_MASK
    ticks = binary & _TICKS_MASK
    day = ticks - ticks % _TICKS_PER_DAY
    return kind | (day + int(round((hours % 24.0) * 36000000000.0)))


def env_packet_info(d):
    """(dateTime, engineTime) if this Entities packet carries the environment, else None."""
    if not d or d[0] != MSG_ENTITIES:
        return None
    for f, w, v in _fields(d[5:]):
        if f == 7 and w == 2:
            sub = _kv(v)
            if 1 in sub:
                return sub[1], sub.get(6)
    return None


def env_rewrite(d, binary=None, engine=None):
    top = _pb_parse(d[5:])
    for it in top:
        if it[0] == 7 and it[1] == 2:
            sub = _pb_parse(it[2])
            if binary is not None:
                _pb_set(sub, 1, 0, binary)
            if engine is not None:
                _pb_set(sub, 6, 5, struct.pack("<f", engine))
            it[2] = _pb_encode(sub)
    return d[:5] + _pb_encode(top)


def demo_set_time(raw, hour_at, every_ms=0, t_end=None):
    """Rewrite the time of day: hour_at(t_ms) -> hours (or None = keep recorded).
    every_ms > 0 adds extra environment snapshots so changes are smooth. Returns (rewritten, added)."""
    env = [(k, p[0], env_packet_info(p[1])) for k, p in enumerate(raw["packets"])]
    env = [(k, t, inf) for k, t, inf in env if inf]
    if not env:
        return 0, 0
    n = 0
    for k, t, (bin_, eng) in env:
        h = hour_at(t)
        if h is not None:
            raw["packets"][k][1] = env_rewrite(raw["packets"][k][1], env_with_hours(bin_, h))
            n += 1
    extra = []
    if every_ms > 0:
        times = [t for _, t, _ in env]
        t_end = t_end if t_end is not None else raw["packets"][-1][0]
        import bisect
        t = times[0] + every_ms
        while t < t_end:
            j = bisect.bisect_right(times, t) - 1
            src_k, src_t, (bin_, eng) = env[j]
            nxt = times[j + 1] if j + 1 < len(times) else None
            if t - src_t >= every_ms / 2 and (nxt is None or nxt - t >= every_ms / 2):
                h = hour_at(t)
                if h is not None:
                    d = env_rewrite(raw["packets"][src_k][1], env_with_hours(bin_, h),
                                    (eng + (t - src_t) / 1000.0) if eng is not None else None)
                    extra.append((t, d))
            t += every_ms
        demo_insert(raw, extra)
    return n, len(extra)


def mat_to_unity_euler(r):
    """Inverse of Unity Quaternion.Euler (R = Ry*Rx*Rz) -> degrees (x, y, z) in [0, 360)."""
    sx = max(-1.0, min(1.0, -r[1][2]))
    x = math.asin(sx)
    if abs(sx) < 0.99999:
        y = math.atan2(r[0][2], r[2][2])
        z = math.atan2(r[1][0], r[1][1])
    else:  # gimbal lock
        y = math.atan2(-r[2][0], r[0][0])
        z = 0.0
    return tuple(math.degrees(a) % 360.0 for a in (x, y, z))


def demo_edit(raw, delete=(), motion=None, flag_keys=None, snapshots=None, clones=(), t_range=None, freeze=()):
    """Apply edits to a raw demo (see read_demo_raw).
    delete:    uids whose packets are all dropped
    motion:    {uid: [(t_ms, pos, euler, server_t)]}  -> replaces original positions of uid inside its key span
    flag_keys: {uid: [(t_ms, flags)]}                 -> replaces original flag messages of uid inside t_range
    snapshots: {uid: dict(pose=f(t_ms)->(pos, euler) | None, flags=f(t_ms)->int | None, grade=int | None)}
    clones:    [dict(packet=bytes, uid, group, pos, euler, t_ms, flags=None, grade=None)]
    """
    delete = set(delete)
    freeze = set(freeze)
    motion = motion or {}
    flag_keys = flag_keys or {}
    snapshots = snapshots or {}
    span = {u: (min(k[0] for k in v), max(k[0] for k in v)) for u, v in motion.items() if v}
    lo, hi = t_range or (0, 1 << 62)
    out, stats = [], {"dropped": 0, "rewritten": 0}
    for t, d, trail in raw["packets"]:
        u = entity_uid(d)
        if u is not None:
            if u in delete:
                stats["dropped"] += 1
                continue
            if d[0] == MSG_POSITION and (u in freeze or (u in span and span[u][0] - 50 <= t <= span[u][1] + 50)):
                stats["dropped"] += 1
                continue
            if d[0] == MSG_FLAGS and u in flag_keys and lo <= t <= hi:
                stats["dropped"] += 1
                continue
            if d[0] == MSG_ENTITIES and u in snapshots:
                sn = snapshots[u]
                pose = sn["pose"](t) if sn.get("pose") else None
                d = entity_rewrite(d, pos=pose[0] if pose else None, euler=pose[1] if pose else None,
                                   flags=sn["flags"](t) if sn.get("flags") else None, grade=sn.get("grade"))
                stats["rewritten"] += 1
        out.append([t, d, trail])
    raw["packets"] = out
    new = []
    for u, keys in motion.items():
        for t, p, e, st in keys:
            new.append((t, pk_entity_position(u, p, e, st)))
    for u, keys in flag_keys.items():
        for t, fl in keys:
            new.append((t, pk_entity_flags(u, fl)))
    for c in clones:
        d = entity_rewrite(c["packet"], uid=c["uid"], group=c.get("group"), pos=c["pos"], euler=c["euler"],
                           flags=c.get("flags"), grade=c.get("grade"), drop_parent=True)
        d = d[:1] + struct.pack("<I", 0xFFFFFFFF) + d[5:]
        new.append((c["t_ms"], d))
    demo_insert(raw, new)
    stats["added"] = len(new)
    return stats



# ---------------------------------------------------------------- Unity <-> Blender (no deps)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def unity_euler_to_quat(x, y, z):
    """Unity Quaternion.Euler: rotate Z, then X, then Y (q = qy*qx*qz)."""
    hx, hy, hz = (math.radians(a) * 0.5 for a in (x, y, z))
    qx = (math.sin(hx), 0.0, 0.0, math.cos(hx))
    qy = (0.0, math.sin(hy), 0.0, math.cos(hy))
    qz = (0.0, 0.0, math.sin(hz), math.cos(hz))
    return _qmul(_qmul(qy, qx), qz)


def quat_to_mat(x, y, z, w):
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def unity_obj_to_blender(pos, euler, scale=(1.0, 1.0, 1.0)):
    """Object transform: M_blender = S * M_unity * S (S swaps Y/Z)."""
    m = quat_to_mat(*unity_euler_to_quat(*euler))
    s = [m[0], m[2], m[1]]
    r = [[row[0], row[2], row[1]] for row in s]
    x, y, z = pos
    sx, sy, sz = scale
    return (x, z, y), r, (sx, sz, sy)


def find_rust_dir():
    """Rust install folder: Steam libraries (libraryfolders.vdf) -> Rust or RustStaging; '' if not found."""
    steam_roots = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)") + r"\Steam",
                   os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Steam",
                   os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam"),
                   os.path.expanduser("~/Library/Application Support/Steam")]
    libs = []
    for root in steam_roots:
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            libs.append(root)
            try:
                with open(vdf, encoding="utf-8", errors="replace") as fh:
                    libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', fh.read())]
            except OSError:
                pass
    found = []
    for lib in libs:
        for name in ("Rust", "RustStaging"):
            d = os.path.join(lib, "steamapps", "common", name)
            m = os.path.join(d, "Bundles", "AssetSceneManifest.json")
            if os.path.isfile(m):
                found.append((os.path.getmtime(m), d))
    return max(found)[1] if found else ""  # both installed -> the most recently updated one


DEFAULT_GAME_DIR = find_rust_dir()  # also settable in the add-on preferences ("Rust folder")


def prefab_id(path):
    """StringPool id: first 4 bytes (LE) of MD5 of the lower-case asset path."""
    import hashlib
    return struct.unpack("<I", hashlib.md5(path.lower().encode("utf-8")).digest()[:4])[0]


def names_from_manifest(manifest_path):
    with open(manifest_path, encoding="utf-8") as fh:
        m = json.load(fh)
    out = {}
    for sc in m.get("Scenes", []):
        for a in sc.get("IncludedAssets", []):
            out[prefab_id(a)] = a.lower()
    return out


def load_prefab_names(*dirs, game_dir=""):
    """id -> asset path. Game folder (Bundles/AssetSceneManifest.json) first, then rust_prefab_names.json."""
    for g in (game_dir, DEFAULT_GAME_DIR):
        mp = os.path.join(g, "Bundles", "AssetSceneManifest.json") if g else ""
        if mp and os.path.isfile(mp):
            try:
                return names_from_manifest(mp)
            except Exception as e:
                print("rust_world: cannot read", mp, e)
    for d in dirs:
        p = os.path.join(d, "rust_prefab_names.json")
        if d and os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                return {int(k): v for k, v in json.load(fh).items()}
    return {}


# ---------------------------------------------------------------- bundle extractor (runs outside Blender)
# python rust_world.py extract --rust <game dir> [--out cache] [--map f.map] [--dem f.dem] [--ids-file ids.txt]
#        [--lod lod0|low] [--hlod] [--no-tex] [--max-tex 1024] [--force]
# Needs UnityPy + Pillow (the Blender panel installs them into a private folder and runs this in a subprocess).
# Bundles are stored uncompressed, so each CAB is opened lazily through mmap (low RAM).
#
# Cache:  prefabs/<id>@<variant>.json   variant = lod0 | low | lod0_hlod | low_hlod
#         meshes/<key>.npz  materials/<key>.json  textures/<key>.png      key = <cab>_<pathid>

LOD_GROUP_RE = re.compile(r"^(.*?)[_ ]?lod[_ ]?(\d+)$", re.I)
BIOME_RE = re.compile(r"_(arid|arctic|tundra|jungle|tropical|temperate)$", re.I)
SKIP_NAMES = {"shadow_proxy", "shadowproxy"}
TEX_WANT = re.compile(r"main|base|albedo|diffuse|color|bump|normal|occlusion|gloss|spec|emission|detail|mask", re.I)
TEX_SKIP = re.compile(r"secondary|particle|blendmask|terrainlayer|biome|wetness|snow|dither|noise|lut", re.I)
REF_FIELDS = ("prefab", "visualprefab", "worldmodelprefab", "model", "clientprefab", "visuals")


EXTRACT_VERSION = 4  # bump to re-walk cached prefabs after extractor changes


def variant_name(lod="lod0", hlod=False):
    return lod + ("_hlod" if hlod else "")


def _extractor_impl():
    """Build the extractor classes (imports UnityPy lazily)."""
    import mmap
    import numpy as np
    from UnityPy.environment import Environment, simplify_name
    from UnityPy.files.SerializedFile import SerializedFile
    from UnityPy.streams import EndianBinaryReader
    try:
        import lz4.block as lz4b
    except ImportError:
        lz4b = None

    def bundle_dir(p):
        with open(p, "rb") as f:
            b = f.read(1 << 20)
        i = b.index(b"\0") + 1
        fmt = struct.unpack(">I", b[i:i + 4])[0]
        i += 4
        i = b.index(b"\0", i) + 1
        i = b.index(b"\0", i) + 1
        size, csz, usz, flags = struct.unpack(">qIII", b[i:i + 20])
        i += 20
        if fmt >= 7:
            i = (i + 15) // 16 * 16
        if flags & 0x80:
            raise ValueError("blocks info at end not supported")
        raw = b[i:i + csz]
        if flags & 0x3F == 0:
            info = raw
        elif lz4b:
            info = lz4b.decompress(raw, uncompressed_size=usz)
        else:
            info = lz4_block_decompress(raw, usz)
        data_off = i + csz
        if flags & 0x200:
            data_off = (data_off + 15) // 16 * 16
        j = 16
        nb = struct.unpack(">i", info[j:j + 4])[0]
        j += 4
        blocks = [struct.unpack(">IIH", info[j + 10 * k:j + 10 * k + 10]) for k in range(nb)]
        j += 10 * nb
        if not all(u == c and (fl & 0x3F) == 0 for u, c, fl in blocks):
            raise ValueError("compressed blocks")
        nn = struct.unpack(">i", info[j:j + 4])[0]
        j += 4
        nodes = []
        for _ in range(nn):
            off, sz, fl = struct.unpack(">qqI", info[j:j + 20])
            j += 20
            e = info.index(b"\0", j)
            nodes.append((data_off + off, sz, fl, info[j:e].decode()))
            j = e + 1
        return nodes

    class LazyEnv(Environment):
        def __init__(self, bundles_root):
            super().__init__()
            self.index = {}
            self._mm = {}
            for dp, dn, fn in os.walk(bundles_root):
                dn[:] = [d for d in dn if d.lower() != "items"]
                for f in fn:
                    if f.endswith(".bundle"):
                        p = os.path.join(dp, f)
                        try:
                            for off, sz, fl, name in bundle_dir(p):
                                self.index[simplify_name(name)] = (p, off, sz, fl)
                        except Exception as e:
                            print("skip bundle", p, e)

        def _view(self, p, off, sz):
            mm = self._mm.get(p)
            if mm is None:
                fh = open(p, "rb")
                mm = self._mm[p] = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            return memoryview(mm)[off:off + sz]

        def get_cab(self, name):
            key = simplify_name(name)
            c = self.cabs.get(key)
            if c is None and key in self.index:
                p, off, sz, fl = self.index[key]
                r = EndianBinaryReader(self._view(p, off, sz))
                c = SerializedFile(r, parent=self, name=key) if fl & 4 else r
                self.cabs[key] = c
            return c

    def tr_local(tr):
        t, q, s = tr.m_LocalPosition, tr.m_LocalRotation, tr.m_LocalScale
        m = np.eye(4)
        m[:3, :3] = np.array(quat_to_mat(q.x, q.y, q.z, q.w)) * np.array([s.x, s.y, s.z])[None, :]
        m[:3, 3] = (t.x, t.y, t.z)
        return m

    def key_of(o):
        return "%s_%d" % (o.assets_file.name.lower().replace("cab-", ""), o.path_id & 0xFFFFFFFFFFFFFFFF)

    def game_manifest_guids(env):
        out = {}
        for name, (p, off, sz, fl) in env.index.items():
            if not fl & 4 or "assetscene" in name or "sharedassets" in name:
                continue
            sf = env.get_cab(name)
            ab = next((o for o in sf.objects.values() if o.type.name == "AssetBundle"), None)
            if ab is None:
                continue
            cont = dict(ab.read().m_Container)
            if "assets/manifest.asset" not in cont:
                continue
            tt = cont["assets/manifest.asset"].asset.deref().read_typetree()
            for e in tt.get("prefabProperties", []) + tt.get("guidPaths", []):
                out[e["guid"]] = e["name"].lower()
            break
        return out

    class Extractor:
        def __init__(self, env, out, scene_of, lod="lod0", hlod=False, textures=True, max_tex=1024, force=False):
            self.env, self.out, self.scene_of = env, out, scene_of
            self.lod, self.hlod, self.textures, self.max_tex, self.force = lod, hlod, textures, max_tex, force
            self.force_mat = False
            self._guids = None
            for d in ("prefabs", "meshes", "materials", "textures"):
                os.makedirs(os.path.join(out, d), exist_ok=True)
            self.roots = {}
            ls = lambda d: set(os.path.splitext(f)[0] for f in os.listdir(os.path.join(out, d)))  # noqa: E731
            self.done_mesh, self.done_mat, self.done_tex = ls("meshes"), ls("materials"), ls("textures")

        def scene_roots(self, scene):
            r = self.roots.get(scene)
            if r is None:
                sf = self.env.get_cab("BuildPlayer-" + scene)
                r = {}
                if sf is not None:
                    for o in sf.objects.values():
                        if o.type.name == "Transform":
                            tr = o.read()
                            if not tr.m_Father or tr.m_Father.path_id == 0:
                                r[tr.m_GameObject.read().m_Name.lower()] = tr
                self.roots[scene] = r
            return r

        # textures are referenced by key; pixels decoded only when wanted (also later, from the key alone)
        def _decode(self, tobj, k, normal):
            if k in self.done_tex or not self.textures:
                return
            try:
                img = tobj.read().image
            except Exception as e:
                print("    texture fail", e)
                return
            if img is None:
                return
            if self.max_tex and max(img.size) > self.max_tex:
                f = self.max_tex / max(img.size)
                img = img.resize((max(1, int(img.size[0] * f)), max(1, int(img.size[1] * f))))
            if normal:
                from PIL import Image
                a = np.asarray(img.convert("RGBA")).astype(np.float32) / 255.0
                r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
                x, y = (al, g) if (r.mean() > 0.9 and al.std() > 0.02) else (r, g)  # DXT5nm / BC5 / plain
                nx, ny = x * 2 - 1, y * 2 - 1
                nz = np.sqrt(np.clip(1 - nx * nx - ny * ny, 0, 1))
                img = Image.fromarray((np.stack([x, y, nz * 0.5 + 0.5], -1) * 255 + 0.5).astype(np.uint8), "RGB")
            img.save(os.path.join(self.out, "textures", k + ".png"), compress_level=3)
            self.done_tex.add(k)

        def texture(self, pptr, normal=False):
            try:
                tobj = pptr.deref()
            except Exception:
                return None
            if tobj is None or tobj.type.name != "Texture2D":
                return None
            k = key_of(tobj) + ("_n" if normal else "")
            self._decode(tobj, k, normal)
            return k

        def texture_by_key(self, k):
            if k in self.done_tex or not self.textures:
                return
            normal = k.endswith("_n")
            base = k[:-2] if normal else k
            cab, pid = base.rsplit("_", 1)
            sf = self.env.get_cab("cab-" + cab) or self.env.get_cab(cab)
            if sf is None:
                return
            pid = int(pid)
            if pid >= 1 << 63:
                pid -= 1 << 64
            o = sf.objects.get(pid)
            if o is not None:
                self._decode(o, k, normal)

        def material(self, pptr):
            try:
                mobj = pptr.deref()
            except Exception:
                return None
            if mobj is None:
                return None
            k = key_of(mobj)
            if k in self.done_mat and not self.force_mat:
                if self.textures:  # cached without pixels earlier -> decode now
                    try:
                        with open(os.path.join(self.out, "materials", k + ".json")) as fh:
                            for t in json.load(fh)["textures"].values():
                                self.texture_by_key(t["tex"])
                    except Exception as e:
                        print("    texture refill fail", e)
                return k
            mat = mobj.read()
            try:
                sh = mat.m_Shader.read()
                shader = sh.m_ParsedForm.m_Name if hasattr(sh, "m_ParsedForm") else getattr(sh, "m_Name", "")
            except Exception:
                shader = ""
            sp = mat.m_SavedProperties
            texs = {}
            for name, te in sp.m_TexEnvs:
                if te.m_Texture.path_id == 0 or not TEX_WANT.search(name) or TEX_SKIP.search(name):
                    continue
                tk = self.texture(te.m_Texture, normal=bool(re.search("normal|bump|nrm", name, re.I)))
                if tk:
                    texs[name] = {"tex": tk, "scale": [te.m_Scale.x, te.m_Scale.y],
                                  "offset": [te.m_Offset.x, te.m_Offset.y]}
            kw = getattr(mat, "m_ValidKeywords", None) or getattr(mat, "m_ShaderKeywords", None) or []
            if isinstance(kw, str):
                kw = kw.split()
            data = {"name": mat.m_Name, "shader": shader, "keywords": list(kw), "textures": texs,
                    "colors": {n: [c.r, c.g, c.b, c.a] for n, c in sp.m_Colors},
                    "floats": {n: v for n, v in sp.m_Floats}}
            with open(os.path.join(self.out, "materials", k + ".json"), "w") as fh:
                json.dump(data, fh)
            self.done_mat.add(k)
            return k

        def resolve(self, owner, ref):
            """typetree PPtr dict {m_FileID, m_PathID} seen from object `owner` -> ObjectReader or None."""
            fid, pid = ref.get("m_FileID", 0), ref.get("m_PathID", 0)
            if not pid:
                return None
            af = owner.assets_file
            if fid:
                try:
                    af = self.env.get_cab(os.path.basename(af.externals[fid - 1].path))
                except Exception:
                    return None
            return af.objects.get(pid) if af is not None and hasattr(af, "objects") else None

        def mesh(self, pptr):
            try:
                mobj = pptr if hasattr(pptr, "path_id") and hasattr(pptr, "assets_file") else pptr.deref()
            except Exception:
                return None
            if mobj is None:
                return None
            k = key_of(mobj)
            if k in self.done_mesh:
                return k
            from UnityPy.helpers.MeshHelper import MeshHandler
            h = MeshHandler(mobj.read())
            h.process()
            if not h.m_Vertices:
                return None
            n = len(h.m_Vertices)
            arr = {"v": np.asarray(h.m_Vertices, np.float32).reshape(n, -1)[:, :3]}
            if h.m_Normals and len(h.m_Normals) == n:
                arr["n"] = np.asarray(h.m_Normals, np.float32).reshape(n, -1)[:, :3]
            if h.m_UV0 and len(h.m_UV0) == n:
                arr["uv"] = np.asarray(h.m_UV0, np.float32).reshape(n, -1)[:, :2]
            if getattr(h, "m_UV1", None) and len(h.m_UV1) == n:
                arr["uv1"] = np.asarray(h.m_UV1, np.float32).reshape(n, -1)[:, :2]
            if getattr(h, "m_Colors", None) and len(h.m_Colors) == n:
                arr["col"] = np.asarray(h.m_Colors, np.float32).reshape(n, -1)[:, :4]
            for si, tris in enumerate(h.get_triangles()):
                arr["i%d" % si] = np.asarray(tris, np.uint32).reshape(-1, 3)
            np.savez_compressed(os.path.join(self.out, "meshes", k + ".npz"), **arr)
            self.done_mesh.add(k)
            return k

        def root_refs(self, root):
            if self._guids is None:
                self._guids = game_manifest_guids(self.env)
            out = []
            for c in root.m_GameObject.read().m_Component:
                try:
                    co = c.component.deref()
                    if co is None or co.type.name != "MonoBehaviour":
                        continue
                    tt = co.read_typetree()
                except Exception:
                    continue
                for k, v in tt.items():
                    if k.lower() in REF_FIELDS and isinstance(v, dict) and v.get("guid") and v["guid"] in self._guids:
                        out.append(self._guids[v["guid"]])
            return out

        @staticmethod
        def _fresh(pj):
            try:
                with open(pj) as fh:
                    return json.load(fh).get("v", 1) >= EXTRACT_VERSION
            except Exception:
                return False

        def building_grades(self, root):
            """ConstructionGrade components -> [{type, skin, name, id, path}], grade prefabs extracted too."""
            out = []
            for c in root.m_GameObject.read().m_Component:
                try:
                    co = c.component.deref()
                    if co is None or co.type.name != "MonoBehaviour":
                        continue
                    if co.read().m_Script.read().m_ClassName != "ConstructionGrade":
                        continue
                    tt = co.read_typetree()
                except Exception:
                    continue
                if self._guids is None:
                    self._guids = game_manifest_guids(self.env)
                path = self._guids.get((tt.get("skinObject") or {}).get("guid", ""))
                gb = self.resolve(co, tt.get("gradeBase") or {})
                try:
                    gbt = gb.read_typetree() if gb is not None else {}
                except Exception:
                    gbt = {}
                if not path:
                    continue
                gid = prefab_id(path)
                scene = self.scene_of.get(path)
                var = variant_name(self.lod, False)
                pj = os.path.join(self.out, "prefabs", "%d@%s.json" % (gid, var))
                if scene and (self.force or not self._fresh(pj)):
                    try:
                        self.prefab(gid, path, scene)
                    except Exception as e:
                        print("    grade fail", path, e)
                out.append({"type": gbt.get("type", 0), "skin": gbt.get("skin", 0), "name": gbt.get("m_Name", ""),
                            "id": gid, "path": path})
            return out

        def collect_conditionals(self, root):
            """ConditionalModel parts in GetComponentsInChildren(true) order (depth-first, inactive included):
            bit i of BuildingBlock.model switches part i on."""
            out = []
            is_skin = False
            for c in root.m_GameObject.read().m_Component:  # only building skins have them
                try:
                    co = c.component.deref()
                    if co is not None and co.type.name == "MonoBehaviour" and \
                            co.read().m_Script.read().m_ClassName == "ConstructionSkin":
                        is_skin = True
                        break
                except Exception:
                    pass
            if not is_skin:
                return out

            def dfs(tr, parent_m):
                m = parent_m @ tr_local(tr)
                go = tr.m_GameObject.read()
                for c in go.m_Component:
                    try:
                        co = c.component.deref()
                        if co is None or co.type.name != "MonoBehaviour":
                            continue
                        cls = co.read().m_Script.read().m_ClassName
                    except Exception:
                        continue
                    if not cls.startswith("ConditionalModel"):
                        continue
                    try:
                        ref = (co.read_typetree().get("prefab") or {}).get("guid", "")
                    except Exception:
                        ref = ""
                    if self._guids is None:
                        self._guids = game_manifest_guids(self.env)
                    out.append({"index": len(out), "name": go.m_Name, "wallpaper": "Wallpaper" in cls,
                                "path": self._guids.get(ref, ""), "matrix": m.ravel().tolist()})
                for ch in tr.m_Children:
                    dfs(ch.read(), m)

            dfs(root, np.eye(4))
            return out

        def _split_lods(self, kids):
            """-> (plain children, [LOD candidates ordered by preference]) for siblings *_LOD0.._LODn."""
            groups, plain = {}, []
            for k in kids:
                m = LOD_GROUP_RE.match(k.m_GameObject.read().m_Name)
                if m:
                    groups.setdefault(m.group(1).lower(), []).append((int(m.group(2)), k))
                else:
                    plain.append(k)
            out = []
            for g in groups.values():
                g.sort(key=lambda x: x[0], reverse=(self.lod == "low"))
                out.append([k for _, k in g])
            return plain, out

        def walk_prefab(self, root, allow_hlod=False):
            nodes = []
            excluded = set()  # renderer path ids switched off by LOD components
            conds = self.collect_conditionals(root)
            self._last_conditionals = conds

            def walk(tr, parent_m, depth, in_hlod):
                go = tr.m_GameObject.read()
                if depth > 0 and not go.m_IsActive:  # templates in AssetScenes have inactive roots
                    return
                name = go.m_Name
                lname = name.lower()
                if lname in SKIP_NAMES or ("hlod" in lname and not in_hlod):
                    return
                m = parent_m @ tr_local(tr)
                comps = {}
                for c in go.m_Component:
                    try:
                        co = c.component.deref()
                    except Exception:
                        continue
                    if co is None:
                        continue
                    tn = co.type.name
                    if tn == "MonoBehaviour":
                        try:
                            tn = "MB:" + co.read().m_Script.read().m_ClassName
                        except Exception:
                            tn = "MB"
                    comps.setdefault(tn, co)
                mf, mr, smr = comps.get("MeshFilter"), comps.get("MeshRenderer"), comps.get("SkinnedMeshRenderer")
                for lodc in ("MB:RendererLOD", "MB:TreeLOD"):  # renderer per distance: keep one, drop the others
                    if lodc in comps:
                        try:
                            st = [x.get("renderer") or {} for x in comps[lodc].read_typetree().get("States", [])]
                            ids = [x.get("m_PathID", 0) for x in st if x.get("m_FileID", 0) == 0]
                            ids_nz = [i for i in ids if i]
                            if ids_nz:
                                keep = ids_nz[-1] if self.lod == "low" else ids_nz[0]
                                excluded.update(i for i in ids_nz if i != keep)
                        except Exception as e:
                            print("    lod states fail", e)
                if in_hlod and "MB:LODMasterMesh" in comps:  # HLOD replacement mesh
                    try:
                        tt = comps["MB:LODMasterMesh"].read_typetree()
                        rm = self.resolve(comps["MB:LODMasterMesh"], tt.get("ReplacementMesh") or {})
                        mk, mats, mm = None, [], m
                        if rm is not None and rm.type.name == "Mesh":
                            mk = self.mesh(rm)
                            mats = [self.material(mp) for mp in mr.read().m_Materials] if mr else []
                        elif rm is not None and rm.type.name in ("MeshRenderer", "SkinnedMeshRenderer"):
                            rend = rm.read()
                            rgo = rend.m_GameObject.read()
                            rmf = next((c.component.deref() for c in rgo.m_Component
                                        if c.component.deref() is not None
                                        and c.component.deref().type.name == "MeshFilter"), None)
                            if rmf is not None:
                                mk = self.mesh(rmf.read().m_Mesh)
                                mats = [self.material(mp) for mp in rend.m_Materials]
                                t_ = next(c.component.read() for c in rgo.m_Component
                                          if c.component.deref() is not None
                                          and c.component.deref().type.name == "Transform")
                                mm = np.eye(4)
                                while t_ is not None:  # world (= prefab-root space) matrix of the renderer
                                    mm = tr_local(t_) @ mm
                                    t_ = t_.m_Father.read() if t_.m_Father and t_.m_Father.path_id else None
                        if mk:
                            nodes.append({"name": name, "matrix": mm.ravel().tolist(), "mesh": mk, "materials": mats})
                            return
                    except Exception as e:
                        print("    hlod master fail", e)
                if mr is not None and mr.path_id in excluded:
                    mr = None
                if (mf and mr) or smr:
                    rend = (mr or smr).read()
                    shadows_only = getattr(rend, "m_CastShadows", 0) == 3 or getattr(rend, "m_ShadowCastingMode", 0) == 3
                    if getattr(rend, "m_Enabled", True) and not shadows_only:
                        try:
                            src = mf.read().m_Mesh if mf else rend.m_Mesh
                            if "MB:MeshLOD" in comps:  # mesh per distance on the same renderer
                                st = [x.get("mesh") or {} for x in comps["MB:MeshLOD"].read_typetree().get("States", [])]
                                st = [x for x in st if x.get("m_PathID")]
                                if st:
                                    alt = self.resolve(comps["MB:MeshLOD"], st[-1] if self.lod == "low" else st[0])
                                    if alt is not None:
                                        src = alt
                            mk = self.mesh(src)
                        except Exception as e:
                            print("    mesh fail", name, e)
                            mk = None
                        if mk:
                            mats = []
                            for mp in rend.m_Materials:
                                try:
                                    mats.append(self.material(mp))
                                except Exception as e:
                                    print("    material fail", e)
                                    mats.append(None)
                            nodes.append({"name": name, "matrix": m.ravel().tolist(), "mesh": mk, "materials": mats})
                kids = [ch.read() for ch in tr.m_Children]
                if "MB:BiomeVisuals" in comps and kids:
                    plain = [k for k in kids if not BIOME_RE.search(k.m_GameObject.read().m_Name)]
                    kids = plain[:1] or kids[:1]
                plain, lod_groups = self._split_lods(kids)
                for k in plain:
                    walk(k, m, depth + 1, in_hlod)
                for cands in lod_groups:  # first LOD (in preference order) that has any mesh; last LODs are often empty
                    for k in cands:
                        n0 = len(nodes)
                        walk(k, m, depth + 1, in_hlod)
                        if len(nodes) > n0:
                            break

            def find_hlod(tr, parent_m, depth, found):
                lname = tr.m_GameObject.read().m_Name.lower()
                if depth > 0 and ("hlod" in lname or "lodmastermesh" in lname):
                    found.append((tr, parent_m))
                    return
                if depth > 6:
                    return
                m = parent_m @ tr_local(tr)
                for ch in tr.m_Children:
                    find_hlod(ch.read(), m, depth + 1, found)

            found = []
            if allow_hlod:  # HLOD = one merged mesh per monument: cheapest silhouette, skip all detail
                find_hlod(root, np.eye(4), 0, found)
            if found:
                for tr, pm in found:
                    walk(tr, pm, 1, True)
            if not nodes:
                walk(root, np.eye(4), 0, False)
            r0 = np.linalg.inv(tr_local(root))
            for n_ in nodes + conds:
                n_["matrix"] = (r0 @ np.array(n_["matrix"]).reshape(4, 4)).ravel().tolist()
            return nodes

        def prefab(self, pid, path, scene, depth_ref=0):
            root = self.scene_roots(scene).get(path.lower())
            if root is None:
                print("  root not found", path)
                return None
            nodes = self.walk_prefab(root, self.hlod and scene.startswith("AssetScene-monument"))
            conds = self._last_conditionals
            refs = []
            if not nodes and depth_ref < 2:  # e.g. TreeEntity.prefab -> client visual prefab
                for ref in self.root_refs(root):
                    sc2 = self.scene_of.get(ref)
                    if sc2 and ref != path.lower():
                        sub = self.prefab(None, ref, sc2, depth_ref + 1)
                        if sub:
                            nodes.extend(sub)
                            refs.append(ref)
            if pid is None:
                return nodes
            grades = self.building_grades(root)
            if grades:  # building block: the root mesh is only collision; looks come from grade prefabs
                nodes = []
            for cd in conds:  # parts switched on per entity by BuildingBlock.model bit mask
                if cd["path"]:
                    cd["id"] = prefab_id(cd["path"])
                    sc_ = self.scene_of.get(cd["path"])
                    pj = os.path.join(self.out, "prefabs", "%d@%s.json" % (cd["id"], variant_name(self.lod, False)))
                    if sc_ and (self.force or not self._fresh(pj)):
                        try:
                            self.prefab(cd["id"], cd["path"], sc_)
                        except Exception as e:
                            print("    part fail", cd["path"], e)
            var = variant_name(self.lod, self.hlod and scene.startswith("AssetScene-monument"))
            with open(os.path.join(self.out, "prefabs", "%d@%s.json" % (pid, var)), "w") as fh:
                json.dump({"v": EXTRACT_VERSION, "id": pid, "path": path.lower(), "scene": scene, "variant": var,
                           "nodes": nodes, "refs": refs, "grades": grades,
                           "conditionals": [c for c in conds if c.get("id")]}, fh)
            return nodes

    return LazyEnv, Extractor


def cached_prefab_file(cache, pid, variant):
    """Best cached json for this prefab: exact variant, then same LOD w/o hlod, then anything."""
    pdir = os.path.join(cache, "prefabs")
    lod = variant.split("_")[0]
    for v in (variant, lod, lod + "_hlod"):
        p = os.path.join(pdir, "%s@%s.json" % (pid, v))
        if os.path.isfile(p):
            return p, v
    old = os.path.join(pdir, "%s.json" % pid)  # v0.2 cache = lod0
    if os.path.isfile(old):
        return old, "lod0"
    for v in ("lod0", "low", "lod0_hlod", "low_hlod"):
        p = os.path.join(pdir, "%s@%s.json" % (pid, v))
        if os.path.isfile(p):
            return p, v
    return None, None


def extract_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="rust_world.py extract")
    ap.add_argument("--rust", default=DEFAULT_GAME_DIR)
    ap.add_argument("--out", default=None)
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--dem", action="append", default=[])
    ap.add_argument("--ids", default="")
    ap.add_argument("--ids-file", default="")
    ap.add_argument("--paths", default="")
    ap.add_argument("--lod", choices=("lod0", "low"), default="lod0")
    ap.add_argument("--hlod", action="store_true", help="monuments: merged HLOD mesh only")
    ap.add_argument("--no-tex", action="store_true")
    ap.add_argument("--max-tex", type=int, default=1024)
    ap.add_argument("--force", action="store_true", help="re-walk prefabs already in cache")
    ap.add_argument("--force-materials", action="store_true")
    ap.add_argument("--budget", type=float, default=0, help="stop after N seconds (re-run to continue)")
    a = ap.parse_args(argv)
    import time
    LazyEnv, Extractor = _extractor_impl()
    bundles = os.path.join(a.rust, "Bundles")
    out = a.out or os.path.join(a.rust, "rust_blender_cache")
    with open(os.path.join(bundles, "AssetSceneManifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    where, scene_of = {}, {}
    for sc in manifest["Scenes"]:
        for p in sc["IncludedAssets"]:
            where[prefab_id(p)] = (p, sc["Name"])
            scene_of[p.lower()] = sc["Name"]
    ids = []
    for m in a.map:
        ids += [p["id"] for p in read_map(m)["prefabs"]]
    for d in a.dem:
        ids += [e["prefab"] for e in read_demo(d)["entities"]]
    ids += [int(x) for x in a.ids.split(",") if x.strip()]
    if a.ids_file:
        with open(a.ids_file) as fh:
            ids += [int(x) for x in fh.read().split() if x.strip()]
    ids += [prefab_id(x.strip()) for x in a.paths.split(",") if x.strip()]
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]
    todo = []
    for i in ids:
        if i not in where:
            continue
        p, sc = where[i]
        var = variant_name(a.lod, a.hlod and sc.startswith("AssetScene-monument"))
        pj = os.path.join(out, "prefabs", "%d@%s.json" % (i, var))
        fresh = False
        if os.path.exists(pj):
            try:
                with open(pj) as fh:
                    fresh = json.load(fh).get("v", 1) >= EXTRACT_VERSION
            except Exception:
                fresh = False
        if not a.force and fresh and a.no_tex:
            continue
        todo.append((i, p, sc, fresh))
    todo.sort(key=lambda x: x[2])
    print("TOTAL %d" % len(todo), flush=True)
    t0 = time.time()
    env = LazyEnv(bundles)
    ex = Extractor(env, out, scene_of, a.lod, a.hlod, not a.no_tex, a.max_tex, a.force)
    ex.force_mat = a.force_materials
    for n, (i, p, sc, exists) in enumerate(todo):
        if a.budget and time.time() - t0 > a.budget:
            print("BUDGET %d left" % (len(todo) - n), flush=True)
            break
        t = time.time()
        if exists and not a.force:  # prefab known: only make sure its textures are there
            try:
                with open(os.path.join(out, "prefabs", "%d@%s.json" % (i, variant_name(
                        a.lod, a.hlod and sc.startswith("AssetScene-monument"))))) as fh:
                    for nd in json.load(fh)["nodes"]:
                        for mk in nd["materials"]:
                            if mk:
                                with open(os.path.join(out, "materials", mk + ".json")) as fm:
                                    for tx in json.load(fm)["textures"].values():
                                        ex.texture_by_key(tx["tex"])
                ok = True
            except Exception as e:
                print("  refill fail", e)
                ok = False
        else:
            try:
                ok = ex.prefab(i, p, sc) is not None
            except Exception:
                import traceback
                traceback.print_exc()
                ok = False
        print("PROGRESS %d %d %s %s %.1fs" % (n + 1, len(todo), "ok" if ok else "ERR", p, time.time() - t), flush=True)
        if n + 1 < len(todo) and todo[n + 1][2] != sc:
            ex.roots.pop(sc, None)
    print("DONE %.0fs" % (time.time() - t0), flush=True)
    return 0


# ---------------------------------------------------------------- Blender

try:
    import bpy
    import mathutils
    import numpy as np
    from bpy.props import StringProperty, IntProperty, BoolProperty, FloatProperty, EnumProperty
    from bpy_extras.io_utils import ImportHelper, ExportHelper
    HAVE_BPY = True
except ImportError:
    HAVE_BPY = False

SPLAT_COLORS = [  # Dirt Snow Sand Rock Grass Forest Stones Gravel (rough albedo)
    (0.30, 0.22, 0.13), (0.90, 0.92, 0.95), (0.75, 0.66, 0.45), (0.33, 0.31, 0.29),
    (0.22, 0.36, 0.11), (0.13, 0.22, 0.08), (0.42, 0.40, 0.37), (0.48, 0.46, 0.43)]
CATEGORY_SIZE = {"Monument": 25.0, "Dungeon": 8.0, "DungeonBase": 8.0}

if HAVE_BPY:

    def _game_dir():
        try:
            return bpy.context.preferences.addons[__name__].preferences.game_dir
        except Exception:
            return ""

    def _collection(name, parent):
        c = bpy.data.collections.get(name)
        if c is None:
            c = bpy.data.collections.new(name)
        if parent is not None and c.name not in parent.children:
            parent.children.link(c)
        return c

    def _placeholder_mesh(human=False):
        name = "RustPlaceholderHuman" if human else "RustPlaceholder"
        me = bpy.data.meshes.get(name)
        if me is None:
            me = bpy.data.meshes.new(name)
            sx, sy, sz = (0.3, 0.2, 1.8) if human else (0.5, 0.5, 1.0)
            v = [(x, y, z) for x in (-sx, sx) for y in (-sy, sy) for z in (0, sz)]
            f = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
            me.from_pydata(v, [], f)
        return me

    def prefab_library(context):
        """Hidden collection holding one collection per prefab id."""
        lib = _collection("RustPrefabLibrary", context.scene.collection)

        def walk(lc):
            if lc.collection == lib:
                return lc
            for ch in lc.children:
                r = walk(ch)
                if r:
                    return r
        lc = walk(context.view_layer.layer_collection)
        if lc:
            lc.exclude = True
        return lib

    def prefab_collection(context, pid, names):
        name = "RustPrefab_%d" % pid
        c = bpy.data.collections.get(name)
        if c is None:
            c = bpy.data.collections.new(name)
            prefab_library(context).children.link(c)
            path = names.get(pid, "")
            c["rust_prefab_id"] = str(pid)
            c["rust_prefab_path"] = path
            human = bool(re.search(r"player\.prefab|/npcplayer/|/npc/|scientist", path))
            ob = bpy.data.objects.new("ph_" + (os.path.splitext(os.path.basename(path))[0] if path else str(pid)),
                                      _placeholder_mesh(human))
            ob.display_type = "SOLID" if human else "WIRE"
            ob.hide_render = not human
            c.objects.link(ob)
        return c

    def _instance(context, coll, pid, names, label, size):
        ob = bpy.data.objects.new(label, None)
        ob.instance_type = "COLLECTION"
        ob.instance_collection = prefab_collection(context, pid, names)
        ob.empty_display_size = size
        coll.objects.link(ob)
        return ob

    def _set_transform(ob, pos, euler, scale=(1, 1, 1)):
        loc, rot, sc = unity_obj_to_blender(pos, euler, scale)
        ob.rotation_mode = "QUATERNION"
        ob.location = loc
        ob.rotation_quaternion = mathutils.Matrix(rot).to_quaternion()
        ob.scale = sc

    def _grid_mesh(name, heights, size, step, mask=None):
        """heights: (res,res) array [z][x] in metres -> Blender mesh (X=x, Y=z, Z=h)."""
        res = heights.shape[0]
        idx = np.arange(0, res, step)
        if idx[-1] != res - 1:
            idx = np.append(idx, res - 1)
        h = heights[np.ix_(idx, idx)]
        n = len(idx)
        coord = idx / (res - 1) * size - size / 2
        X, Y = np.meshgrid(coord, coord)  # Y rows = z
        verts = np.stack([X, Y, h], -1).reshape(-1, 3).astype(np.float32)
        a = (np.arange(n - 1)[:, None] * n + np.arange(n - 1)[None, :]).ravel()
        quads = np.stack([a, a + 1, a + n + 1, a + n], -1)
        if mask is not None:
            m = mask[np.ix_(idx, idx)]
            qm = (m[:-1, :-1] & m[:-1, 1:] & m[1:, :-1] & m[1:, 1:]).ravel()
            quads = quads[qm]
            used = np.unique(quads)
            remap = np.full(len(verts), -1, np.int64)
            remap[used] = np.arange(len(used))
            verts = verts[used]
            quads = remap[quads]
        me = bpy.data.meshes.new(name)
        me.vertices.add(len(verts))
        me.vertices.foreach_set("co", verts.ravel())
        me.loops.add(quads.size)
        me.loops.foreach_set("vertex_index", quads.ravel().astype(np.int32))
        me.polygons.add(len(quads))
        me.polygons.foreach_set("loop_start", (np.arange(len(quads)) * 4).astype(np.int32))
        if bpy.app.version < (4, 0, 0):
            me.polygons.foreach_set("loop_total", np.full(len(quads), 4, np.int32))
        uv = me.uv_layers.new(name="UVMap")
        lv = verts[quads.ravel()]
        uvs = np.stack([lv[:, 0] / size + 0.5, lv[:, 1] / size + 0.5], -1).astype(np.float32)
        uv.data.foreach_set("uv", uvs.ravel())
        me.update(calc_edges=True)
        me.validate()
        try:
            me.shade_smooth()
        except AttributeError:
            me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), bool))
        return me

    def _fcurves(ob):
        ad = ob.animation_data
        act = ad.action if ad else None
        if act is None:
            return []
        if hasattr(act, "fcurves"):  # Blender < 5.0
            return list(act.fcurves)
        from bpy_extras import anim_utils  # slotted actions (4.4+ / 5.x)
        cb = anim_utils.action_get_channelbag_for_slot(act, ad.action_slot)
        return list(cb.fcurves) if cb else []

    def _image(name, rgba):
        hgt, wid = rgba.shape[:2]
        img = bpy.data.images.get(name)
        if img is not None:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(name, wid, hgt, alpha=True)
        img.pixels.foreach_set(rgba.astype(np.float32).ravel())
        img.pack()
        return img

    def _terrain_material(splat):
        S = splat.astype(np.float32) / 255.0  # (8, r, r)
        res = S.shape[1]
        rgb = np.tensordot(S.transpose(1, 2, 0), np.array(SPLAT_COLORS, np.float32), axes=1)
        alb = np.concatenate([rgb, np.ones((res, res, 1), np.float32)], -1)
        img = _image("RustTerrainAlbedo", alb)
        c0 = _image("RustSplat_0-3", S[0:4].transpose(1, 2, 0))  # Dirt Snow Sand Rock
        c1 = _image("RustSplat_4-7", S[4:8].transpose(1, 2, 0))  # Grass Forest Stones Gravel
        for c in (c0, c1):
            c.colorspace_settings.name = "Non-Color"
            c.alpha_mode = "CHANNEL_PACKED"
        mat = bpy.data.materials.new("RustTerrain")
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, 300)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.9
        for k, c in enumerate((c0, c1)):  # kept for building a real splat shader later
            t = nt.nodes.new("ShaderNodeTexImage")
            t.image = c
            t.location = (-400, -50 - 280 * k)
            t.label = c.name
        return mat

    def _water_material():
        mat = bpy.data.materials.get("RustWater")
        if mat is None:
            mat = bpy.data.materials.new("RustWater")
            mat.use_nodes = True
            b = mat.node_tree.nodes.get("Principled BSDF")
            b.inputs["Base Color"].default_value = (0.02, 0.08, 0.1, 1)
            b.inputs["Roughness"].default_value = 0.05
            mat.diffuse_color = (0.1, 0.3, 0.45, 0.7)
        return mat

    def import_map(context, path, step=2, do_water=True, do_prefabs=True, do_paths=True, skip_decor=False):
        w = read_map(path)
        size = float(w["size"])
        names = load_prefab_names(os.path.dirname(path), os.path.dirname(__file__), game_dir=_game_dir())
        root = _collection(os.path.splitext(os.path.basename(path))[0][:60], context.scene.collection)
        root["rust_map_size"] = size
        rh = short_res(w, "height")
        H = np.frombuffer(w["maps"]["height"], "<u2").reshape(rh, rh).astype(np.float32) / 32766.0 * 1000.0 - 500.0

        me = _grid_mesh("RustTerrain", H, size, step)
        if "splat" in w["maps"]:
            sr = int(round(math.sqrt(len(w["maps"]["splat"]) // 8)))
            me.materials.append(_terrain_material(np.frombuffer(w["maps"]["splat"], "u1").reshape(8, sr, sr)))
        terr = bpy.data.objects.new("RustTerrain", me)
        root.objects.link(terr)

        if do_water:
            wm = _water_material()
            ocean = bpy.data.meshes.new("RustOcean")
            s = size * 1.5
            ocean.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
            ocean.materials.append(wm)
            root.objects.link(bpy.data.objects.new("RustOcean", ocean))
            if "water" in w["maps"]:
                rw = short_res(w, "water")
                Wv = np.frombuffer(w["maps"]["water"], "<u2").reshape(rw, rw).astype(np.float32)
                Wh = Wv / 32766.0 * 1000.0 - 500.0
                Ht = H if rw == rh else Wh * 0
                mask = (Wv > 0) & (Wh > 0.5) & (Wh > Ht - 2.0)
                if mask.any():
                    lm = _grid_mesh("RustLakesRivers", Wh, size, step, mask)
                    if len(lm.polygons):
                        lm.materials.append(wm)
                        root.objects.link(bpy.data.objects.new("RustLakesRivers", lm))

        if do_paths and w["paths"]:
            pc = _collection("Paths", root)
            for p in w["paths"]:
                if len(p["nodes"]) < 2:
                    continue
                cu = bpy.data.curves.new(p["name"], "CURVE")
                cu.dimensions = "3D"
                sp = cu.splines.new("POLY")
                sp.points.add(len(p["nodes"]) - 1)
                for pt, (x, y, z) in zip(sp.points, p["nodes"]):
                    pt.co = (x, z, y, 1.0)
                ob = bpy.data.objects.new(p["name"], cu)
                ob["rust_width"] = p["width"]
                pc.objects.link(ob)

        if do_prefabs:
            pcs = {}
            for pf in w["prefabs"]:
                cat = pf["category"] or "Prefab"
                if skip_decor and cat == "Decor":
                    continue
                coll = pcs.get(cat)
                if coll is None:
                    coll = pcs[cat] = _collection("%s (%s)" % (cat, root.name), root)
                path_ = names.get(pf["id"], "")
                label = os.path.splitext(os.path.basename(path_))[0] if path_ else "%s_%d" % (cat, pf["id"])
                ob = _instance(context, coll, pf["id"], names, label, CATEGORY_SIZE.get(cat, 2.0))
                ob["rust_category"] = cat
                _set_transform(ob, pf["pos"], pf["rot"], pf["scale"])
        _far_viewports(size * 3)
        return root

    def _far_viewports(dist):
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    for sp in area.spaces:
                        if sp.type == "VIEW_3D" and sp.clip_end < dist:
                            sp.clip_end = dist

    NATURE_RE = re.compile(r"/autospawn/(resource|collectable)/|/resource/|/ores?/|/rust\.ai/agents/.*\.corpse")

    def entity_category(path):
        if "/building core/" in path or "/building/" in path:
            return "Building"
        if "/deployable/" in path:
            return "Deployable"
        if NATURE_RE.search(path):
            return "Nature"
        if "/vehicle" in path:
            return "Vehicle"
        return "Entity"

    def _motion_sig(ob):
        """Fingerprint of an object's motion (keys or static transform) to spot edits at export."""
        import hashlib
        parts = [ob.parent.name if ob.parent else ""]
        if ob.parent is not None and not ob.parent.get("rust_uid") and _motion_range(ob.parent):
            parts.append("animated-parent")
        anim = [fc for fc in _fcurves(ob) if fc.data_path in ("location", "rotation_quaternion", "rotation_euler")]
        if anim:
            for fc in sorted(anim, key=lambda f: (f.data_path, f.array_index)):
                parts.append("%s%d:" % (fc.data_path, fc.array_index) +
                             ",".join("%.4f/%.5f" % tuple(kp.co) for kp in fc.keyframe_points))
        else:
            parts.append(",".join("%.4f" % v for row in ob.matrix_basis for v in row))
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _flag_sig(ob):
        import hashlib
        parts = []
        for prop in ("rust_open", "rust_on", "rust_locked"):
            if prop in ob.keys():
                parts.append("%s=%s" % (prop, int(ob[prop])))
        for fc in _fcurves(ob):
            if fc.data_path in ('["rust_open"]', '["rust_on"]', '["rust_locked"]'):
                parts.append(fc.data_path + ",".join("%.3f/%.1f" % tuple(kp.co) for kp in fc.keyframe_points))
        return hashlib.md5("|".join(sorted(parts)).encode()).hexdigest()

    def import_demo(context, path, start=0.0, skip_static=False, do_ticks=True, entities=None):
        """entities: ALL | NO_NATURE (skip trees/ores/collectables) | MOVING (players, NPCs, vehicles)."""
        entities = entities or ("MOVING" if skip_static else "ALL")
        demo = read_demo(path)
        scene = context.scene
        fps = scene.render.fps / scene.render.fps_base
        names = load_prefab_names(os.path.dirname(path), os.path.dirname(__file__), game_dir=_game_dir())
        root = _collection("Demo " + os.path.splitext(os.path.basename(path))[0], context.scene.collection)
        root["rust_demo_start"] = start
        f0 = scene.frame_start

        def frame(ts):
            return f0 + (ts - start) * fps

        dyn = _collection("Moving (%s)" % root.name, root)
        sta = _collection("Static (%s)" % root.name, root)
        ents = {e["uid"]: e for e in demo["entities"] if e["keys"]}
        moving = {u for u, e in ents.items() if len(e["keys"]) > 1 or e["name"]}
        if entities == "MOVING":
            keep = set(moving)
        elif entities == "NO_NATURE":
            keep = {u for u, e in ents.items() if u in moving or not NATURE_RE.search(names.get(e["prefab"], ""))}
        else:
            keep = set(ents)
        changed = True
        while changed:  # keep parents of kept entities and static children of kept parents
            changed = False
            for u, e in ents.items():
                p = e["parent"]
                if u in keep and p in ents and p not in keep:
                    keep.add(p)
                    changed = True
                elif u not in keep and p in keep:
                    keep.add(u)
                    changed = True
        by_uid = {}
        made = []
        for e in demo["entities"]:
            if e["uid"] not in keep or not e["keys"]:
                continue
            moving_ = e["uid"] in moving
            path_ = names.get(e["prefab"], "")
            label = e["name"] or (os.path.splitext(os.path.basename(path_))[0] if path_ else "ent_%d" % e["prefab"])
            ob = _instance(context, dyn if moving_ else sta, e["prefab"], names, label, 1.0)
            ob["rust_uid"] = str(e["uid"])
            ob["rust_prefab"] = str(e["prefab"])
            ob["rust_category"] = "Player/NPC" if e["name"] else entity_category(path_)
            if e.get("grade") is not None:
                ob["rust_grade"] = int(e["grade"])
                ob["rust_model"] = int(e.get("model", 0))
            if e["userid"]:
                ob["rust_userid"] = str(e["userid"])
            by_uid[e["uid"]] = ob
            made.append((e, ob))
        for e, ob in made:  # parenting: child keys are local to the parent
            par = by_uid.get(e["parent"])
            if par is not None:
                ob.parent = par
        for e, ob in made:
            prev = None
            keys = e["keys"]
            for ts, p, r in keys:
                _set_transform(ob, p, r)
                q = ob.rotation_quaternion.copy()
                if prev is not None and prev.dot(q) < 0:
                    q.negate()
                    ob.rotation_quaternion = q
                prev = q
                if len(keys) > 1:
                    ob.keyframe_insert("location", frame=frame(ts))
                    ob.keyframe_insert("rotation_quaternion", frame=frame(ts))
            for attr in ("hide_viewport", "hide_render"):
                setattr(ob, attr, True)
                ob.keyframe_insert(attr, frame=frame(e["spawn"]) - 1)
                setattr(ob, attr, False)
                ob.keyframe_insert(attr, frame=frame(e["spawn"]))
                if e["destroy"] is not None:
                    setattr(ob, attr, True)
                    ob.keyframe_insert(attr, frame=frame(e["destroy"]))
                    setattr(ob, attr, False)
            for fc in _fcurves(ob):
                interp = "CONSTANT" if fc.data_path.startswith("hide") else "LINEAR"
                for kp in fc.keyframe_points:
                    kp.interpolation = interp
            ob["rust_demo"] = path  # for demo export: what changed vs. the recording
            ob["rust_name"] = ob.name
            hist = e.get("flag_hist") or [(e["spawn"], int(e.get("flags", 0)))]
            ob["rust_flags"] = int(hist[0][1])
            opens = [(t, bool(f & FLAG_OPEN)) for t, f in hist]
            if is_door_path(names.get(e["prefab"], "")) or len({v for _, v in opens}) > 1:
                ob["rust_open"] = int(opens[0][1])
                if len({v for _, v in opens}) > 1:
                    prev = None
                    for t, v in opens:
                        if v != prev:
                            ob["rust_open"] = int(v)
                            ob.keyframe_insert('["rust_open"]', frame=frame(t))
                            prev = v
                    for fc in _fcurves(ob):
                        if fc.data_path == '["rust_open"]':
                            for kp in fc.keyframe_points:
                                kp.interpolation = "CONSTANT"
            ob["rust_flag_sig"] = _flag_sig(ob)
            ob["rust_group"] = int(e.get("group", 0))
            ob["rust_sig"] = _motion_sig(ob)

        if do_ticks and demo["ticks"]:  # recorder's own movement (eye position + aim)
            ob = bpy.data.objects.new("Recorder_tick", None)
            ob.empty_display_type = "SINGLE_ARROW"
            ob.empty_display_size = 1.0
            root.objects.link(ob)
            ob.rotation_mode = "QUATERNION"
            prev = None
            for ts, p, aim in demo["ticks"]:
                loc, rot, _ = unity_obj_to_blender(p, (aim[0], aim[1], 0.0))
                q = (mathutils.Matrix(rot) @ mathutils.Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))).to_quaternion()
                if prev is not None and prev.dot(q) < 0:
                    q.negate()
                prev = q
                ob.location = loc
                ob.rotation_quaternion = q
                ob.keyframe_insert("location", frame=frame(ts))
                ob.keyframe_insert("rotation_quaternion", frame=frame(ts))
        L = demo["header"]["length_ms"] / 1000.0
        scene.frame_end = max(scene.frame_end, int(frame(L)))
        root["rust_recorder"] = str(demo["header"]["recorder"])
        root["rust_demo_path"] = path
        root["rust_frame0"] = f0
        root["rust_fps"] = fps
        root["rust_uids"] = json.dumps(sorted(int(e["uid"]) for e, _ in made))
        try:  # recorded time of day (environment snapshots) for display / defaults
            env = []
            for t, d, _ in read_demo_raw(path)["packets"]:
                inf = env_packet_info(d)
                if inf:
                    env.append([round(t / 1000.0, 3), round(env_hours(inf[0]), 4)])
            root["rust_env"] = json.dumps(env)
            if env and hasattr(scene, "rust"):
                k = min(range(len(env)), key=lambda i: abs(env[i][0] - start))
                scene.rust.hour = env[k][1]
        except Exception as ex:
            print("rust_world: no environment info", ex)
        return root

    class IMPORT_OT_rust_map(bpy.types.Operator, ImportHelper):
        bl_idname = "import_scene.rust_map"
        bl_label = "Import Rust map"
        filename_ext = ".map"
        filter_glob: StringProperty(default="*.map", options={"HIDDEN"})
        step: EnumProperty(name="Terrain detail", default="2", items=[
            ("1", "Full (1 m on 2k map)", ""), ("2", "1/2", ""), ("4", "1/4", ""), ("8", "1/8", "")])
        water: BoolProperty(name="Water", default=True)
        prefabs: BoolProperty(name="Prefabs (placeholders)", default=True)
        skip_decor: BoolProperty(name="Skip Decor prefabs", default=False)
        paths: BoolProperty(name="Roads / rivers / rails", default=True)

        def execute(self, context):
            import_map(context, self.filepath, int(self.step), self.water, self.prefabs, self.paths, self.skip_decor)
            return {"FINISHED"}

    class IMPORT_OT_rust_demo(bpy.types.Operator, ImportHelper):
        bl_idname = "import_scene.rust_demo"
        bl_label = "Import Rust demo"
        filename_ext = ".dem"
        filter_glob: StringProperty(default="*.dem", options={"HIDDEN"})
        start: FloatProperty(name="Demo time at start frame (s)", default=-1.0, min=-1.0,
                             description="-1 = take rust_start from the active shot camera, else 0")
        entities: EnumProperty(name="Entities", default="ALL", items=[
            ("ALL", "All", "Buildings, doors, deployables, trees, players..."),
            ("NO_NATURE", "All except nature", "Skip trees, ores, collectables"),
            ("MOVING", "Moving only", "Players, NPCs, animals, vehicles")])
        ticks: BoolProperty(name="Recorder eyes (own ticks)", default=True)

        def execute(self, context):
            st = self.start
            if st < 0:
                cam = context.scene.camera
                st = float(cam.get("rust_start", 0.0)) if cam else 0.0
            import_demo(context, self.filepath, st, False, self.ticks, self.entities)
            return {"FINISHED"}

    # ------------------------------------------------------------ prefab assets from rust_extract.py cache

    _S4 = None

    def _s4():
        global _S4
        if _S4 is None:
            _S4 = mathutils.Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
        return _S4

    def _cache_dir():
        try:
            c = bpy.context.preferences.addons[__name__].preferences.cache_dir
        except Exception:
            c = ""
        return bpy.path.abspath(c) if c else os.path.join(_game_dir() or DEFAULT_GAME_DIR, "rust_blender_cache")

    def _cache_image(cache, key, non_color):
        name = "rt_" + key
        img = bpy.data.images.get(name)
        if img is None:
            p = os.path.join(cache, "textures", key + ".png")
            if not os.path.isfile(p):
                return None
            img = bpy.data.images.load(p, check_existing=True)
            img.name = name
            if non_color:
                img.colorspace_settings.name = "Non-Color"
        return img

    def _cache_material(cache, key):
        name = "rm_" + key
        mat = bpy.data.materials.get(name)
        if mat is not None and not mat.get("rust_tex_missing"):
            return mat
        p = os.path.join(cache, "materials", key + ".json")
        if not os.path.isfile(p):
            return mat
        with open(p) as fh:
            d = json.load(fh)
        miss = sum(1 for t in d["textures"].values()
                   if not os.path.isfile(os.path.join(cache, "textures", t["tex"] + ".png")))
        if mat is not None:
            if miss >= mat["rust_tex_missing"]:
                return mat  # nothing new on disk
            mat.node_tree.nodes.clear()  # rebuild with the textures that arrived
            out = mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
            out.location = (300, 0)
            pb = mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
            mat.node_tree.links.new(pb.outputs[0], out.inputs[0])
        else:
            mat = bpy.data.materials.new(name)
        mat["rust_tex_missing"] = miss
        mat["rust_name"] = d["name"]
        mat["rust_shader"] = d["shader"]
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        tx, cols, fl = d["textures"], d["colors"], d["floats"]
        kw = " ".join(d.get("keywords", [])).upper()
        shader = d["shader"].lower()
        y = 300

        def tex(prop, non_color=False):
            nonlocal y
            t = tx.get(prop)
            if not t:
                return None
            img = _cache_image(cache, t["tex"], non_color)
            if img is None:
                return None
            n = nt.nodes.new("ShaderNodeTexImage")
            n.image = img
            n.location = (-700, y)
            y -= 300
            sc, of = t.get("scale", [1, 1]), t.get("offset", [0, 0])
            if sc != [1, 1] or of != [0, 0]:
                mp = nt.nodes.new("ShaderNodeMapping")
                mp.location = (-900, y + 300)
                mp.inputs["Scale"].default_value = (sc[0], sc[1], 1)
                mp.inputs["Location"].default_value = (of[0], of[1], 0)
                uv = nt.nodes.new("ShaderNodeTexCoord")
                uv.location = (-1100, y + 300)
                nt.links.new(uv.outputs["UV"], mp.inputs["Vector"])
                nt.links.new(mp.outputs["Vector"], n.inputs["Vector"])
            return n

        col = cols.get("_Color") or cols.get("_BaseColor") or [1, 1, 1, 1]
        mat.diffuse_color = (col[0], col[1], col[2], 1.0)
        main = tex("_MainTex") or tex("_BaseMap")
        if main is None:  # other shaders (Core/Fur, ...): first albedo-like map
            for prop in sorted(tx):
                if re.search("albedo|basecolor|base_color|diffuse|colormap", prop, re.I) and "detail" not in prop.lower():
                    main = tex(prop)
                    break
        if main:
            mix = nt.nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            mix.blend_type = "MULTIPLY"
            mix.location = (-350, 300)
            mix.inputs["Factor"].default_value = 1.0
            nt.links.new(main.outputs["Color"], mix.inputs[6])
            mix.inputs[7].default_value = col
            nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
            decal = "decal" in shader
            cut = decal or "ALPHATEST" in kw or "cutout" in shader or "foliage" in shader or "leaves" in shader \
                or "wind" in shader or fl.get("_Mode", 0) in (1.0, 2.0, 3.0)
            if cut:
                nt.links.new(main.outputs["Alpha"], bsdf.inputs["Alpha"])
                if decal:  # decals also fade by alpha; keep them off the shadow pass
                    try:
                        mat.surface_render_method = "BLENDED"
                    except (AttributeError, TypeError):
                        pass
                elif hasattr(mat, "blend_method"):
                    try:
                        mat.blend_method = "CLIP"
                    except TypeError:
                        pass
        else:
            bsdf.inputs["Base Color"].default_value = col
        nrm = tex("_BumpMap", True) or tex("_NormalMap", True)
        if nrm:
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-350, -300)
            nm.inputs["Strength"].default_value = fl.get("_BumpScale", 1.0)
            nt.links.new(nrm.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        gloss = fl.get("_Glossiness", fl.get("_Smoothness", 0.3))
        gmap = tex("_SpecGlossMap", True) or tex("_MetallicGlossMap", True)
        if gmap:
            inv = nt.nodes.new("ShaderNodeMath")
            inv.operation = "MULTIPLY_ADD"  # 1 - a*scale
            inv.location = (-350, 0)
            inv.inputs[1].default_value = -fl.get("_GlossMapScale", 1.0)
            inv.inputs[2].default_value = 1.0
            nt.links.new(gmap.outputs["Alpha"], inv.inputs[0])
            nt.links.new(inv.outputs[0], bsdf.inputs["Roughness"])
            if "_MetallicGlossMap" in tx and "_SpecGlossMap" not in tx:
                sep = nt.nodes.new("ShaderNodeSeparateColor")
                sep.location = (-350, 120)
                nt.links.new(gmap.outputs["Color"], sep.inputs[0])
                nt.links.new(sep.outputs[0], bsdf.inputs["Metallic"])
        else:
            bsdf.inputs["Roughness"].default_value = 1.0 - gloss
        if "_Metallic" in fl and "_MetallicGlossMap" not in tx:
            bsdf.inputs["Metallic"].default_value = fl["_Metallic"]
        emi = cols.get("_EmissionColor")
        if emi and "EMISSION" in kw and max(emi[:3]) > 0:
            et = tex("_EmissionMap")
            ein = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
            if et:
                nt.links.new(et.outputs["Color"], ein)
            else:
                ein.default_value = emi
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = 1.0
        return mat

    def _cache_mesh(cache, key, mats):
        name = "rmesh_" + key
        me = bpy.data.meshes.get(name)
        if me is not None:
            return me
        p = os.path.join(cache, "meshes", key + ".npz")
        if not os.path.isfile(p):
            return None
        z = np.load(p)
        v = z["v"][:, [0, 2, 1]]  # Unity (x,y,z) -> Blender (x,z,y)
        subs = sorted((k for k in z.files if k.startswith("i") and k[1:].isdigit()), key=lambda k: int(k[1:]))
        tris, mi = [], []
        for si, k in enumerate(subs):
            t = z[k].reshape(-1, 3)[:, ::-1]  # the axis swap mirrors -> reverse winding
            tris.append(t)
            mi.append(np.full(len(t), si, np.int32))
        if not tris:
            return None
        tris = np.concatenate(tris).astype(np.int32)
        mi = np.concatenate(mi)
        ok = (tris < len(v)).all(1)
        tris, mi = tris[ok], mi[ok]
        me = bpy.data.meshes.new(name)
        me.vertices.add(len(v))
        me.vertices.foreach_set("co", v.astype(np.float32).ravel())
        me.loops.add(tris.size)
        me.loops.foreach_set("vertex_index", tris.ravel())
        me.polygons.add(len(tris))
        me.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
        if bpy.app.version < (4, 0, 0):
            me.polygons.foreach_set("loop_total", np.full(len(tris), 3, np.int32))
        me.polygons.foreach_set("material_index", mi)
        for uvk, uvname in (("uv", "UVMap"), ("uv1", "UV2")):
            if uvk in z.files:
                uv = me.uv_layers.new(name=uvname)
                uv.data.foreach_set("uv", z[uvk][tris.ravel()].astype(np.float32).ravel())
        if "col" in z.files:
            try:
                ca = me.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
                ca.data.foreach_set("color", z["col"].astype(np.float32).ravel())
            except Exception:
                pass
        me.update(calc_edges=True)
        me.validate(clean_customdata=False)
        if "n" in z.files and len(me.vertices) == len(v):
            try:
                me.shade_smooth()
            except AttributeError:
                me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), bool))
            if bpy.app.version < (4, 1, 0):
                me.use_auto_smooth = True
            n = z["n"].reshape(len(v), -1)[:, [0, 2, 1]]
            me.normals_split_custom_set_from_vertices([tuple(x) for x in n])
        for _ in range(len(subs)):
            me.materials.append(None)
        for si, mk in enumerate(mats[:len(subs)]):
            if mk:
                me.materials[si] = _cache_material(cache, mk)
        return me

    def prefab_instances(scene=None):
        """Placed prefab instances (map + demo): objects instancing a RustPrefab_* collection."""
        objs = scene.objects if scene else bpy.data.objects
        return [o for o in objs if (o.instance_type == "COLLECTION" or o.get("rust_swing")) and o.instance_collection
                and o.instance_collection.name.startswith(("RustPrefab_", "RustCombo_"))
                and not any(c.name.startswith(("RustCombo_", "RustPrefab")) for c in o.users_collection)]

    def _pid_of(coll):
        return str(coll.get("rust_prefab_id") or coll.name[len("RustPrefab_"):])

    DEFAULT_PARTS = ("full", "roof.full", "wall_e", "wall_s", "wall_w", "wall_n")

    def _select_parts(gpath, conds, mask):
        """Parts to show for one building block. Wall-type blocks: the networked bit mask maps 1:1 onto the
        client's ConditionalModel list. Others (foundations, roofs): the server list also has server-only
        entries that are stripped from client bundles, so bits don't line up -> sensible default set."""
        nonwp = [c for c in conds if not c.get("wallpaper")]
        if "/wall" in gpath and mask.bit_length() <= len(conds):
            return [c for c in conds if (mask >> c["index"]) & 1]
        return [c for c in nonwp if c["name"] in DEFAULT_PARTS]

    def _combo_collection(context, gid, gpath, conds, mask, with_base=True):
        """Grade prefab + its conditional parts chosen for this block (walls: full/left/right/corners...)."""
        sel = _select_parts(gpath, conds, mask)
        name = "RustCombo_%s_%s" % (gid, "-".join(str(c["index"]) for c in sel) or "none")
        c = bpy.data.collections.get(name)
        if c is not None:
            return c, [str(cd["id"]) for cd in sel] + ([str(gid)] if with_base else [])
        c = bpy.data.collections.new(name)
        prefab_library(context).children.link(c)
        c["rust_prefab_id"] = str(gid)
        c["rust_prefab_path"] = gpath
        c["rust_model"] = mask
        S = _s4()
        parts = []
        if with_base:  # grade prefab's own meshes (most walls have none - only parts)
            base = prefab_collection(context, int(gid), {int(gid): gpath})
            e = bpy.data.objects.new("base", None)
            e.instance_type = "COLLECTION"
            e.instance_collection = base
            c.objects.link(e)
            parts.append(str(gid))
        for cd in sel:
            pc = prefab_collection(context, int(cd["id"]), {int(cd["id"]): cd["path"]})
            e = bpy.data.objects.new(cd["name"], None)
            e.instance_type = "COLLECTION"
            e.instance_collection = pc
            e.matrix_world = S @ mathutils.Matrix([cd["matrix"][i * 4:i * 4 + 4] for i in range(4)]) @ S
            c.objects.link(e)
            parts.append(str(cd["id"]))
        return c, parts

    def load_prefab_assets(context, cache=None, only_missing=True, ids=None, variant="lod0"):
        """Fill RustPrefab_* collections from the cache. ids: set of prefab ids (str) or None = all."""
        cache = cache or _cache_dir()
        if not os.path.isdir(os.path.join(cache, "prefabs")):
            raise RuntimeError("no cache at %s - extract assets first" % cache)
        S = _s4()
        done = missing = 0
        for c in list(bpy.data.collections):
            if not c.name.startswith("RustPrefab_"):
                continue
            pid = _pid_of(c)
            if ids is not None and pid not in ids:
                continue
            p, v = cached_prefab_file(cache, pid, variant)
            if p is None:
                missing += 1
                continue
            mats_missing = any(m.get("rust_tex_missing") for ob in c.objects if ob.type == "MESH"
                               for m in ob.data.materials if m)
            if only_missing and c.get("rust_variant") == v and not mats_missing:
                continue
            with open(p) as fh:
                d = json.load(fh)
            nodes = d["nodes"] if v.endswith("_hlod") else [n for n in d["nodes"] if "hlod" not in n["name"].lower()]
            c["rust_variant"] = v
            c["rust_prefab_path"] = d.get("path", "")
            if not nodes:  # nothing visual (players are dressed at runtime, server-only prefabs): keep placeholder
                c["rust_loaded"] = True
                continue
            old_meshes = set()
            for ob in list(c.objects):
                if ob.name.startswith("ph_") or ob.get("rust_asset"):
                    if ob.type == "MESH":
                        old_meshes.add(ob.data)
                    bpy.data.objects.remove(ob)
            for n in nodes:
                me = _cache_mesh(cache, n["mesh"], n["materials"])
                if me is None:
                    continue
                for si, mk in enumerate(n["materials"][:len(me.materials)]):  # pick up late textures
                    if mk and me.materials[si] is not None and me.materials[si].get("rust_tex_missing"):
                        _cache_material(cache, mk)
                ob = bpy.data.objects.new(n["name"], me)
                ob["rust_asset"] = True
                mu = mathutils.Matrix([n["matrix"][i * 4:i * 4 + 4] for i in range(4)])
                ob.matrix_world = S @ mu @ S
                c.objects.link(ob)
            for me in old_meshes:  # free meshes of the previous LOD
                if me.users == 0:
                    bpy.data.meshes.remove(me)
            c["rust_loaded"] = True
            done += 1
        return done, missing

    def apply_building_grades(context, cache=None, variant="lod0"):
        """Demo building blocks: point each instance at the prefab of its grade (twig/wood/stone/metal/armored).
        Returns number of swapped instances; grade prefabs are loaded from the cache."""
        cache = cache or _cache_dir()
        tables, need, n = {}, set(), 0
        for ob in prefab_instances(context.scene):
            if "rust_grade" not in ob:
                continue
            bpid = str(ob.get("rust_block_pid") or _pid_of(ob.instance_collection))
            if bpid not in tables:
                p, v = cached_prefab_file(cache, bpid, variant)
                g = []
                if p:
                    try:
                        with open(p) as fh:
                            g = json.load(fh).get("grades") or []
                    except Exception:
                        g = []
                tables[bpid] = g
            grades = tables[bpid]
            if not grades:
                continue
            gr, sk = int(ob["rust_grade"]), int(ob.get("rust_grade_skin", 0))
            pick = next((g for g in grades if g["type"] == gr and g["skin"] == sk), None) \
                or next((g for g in grades if g["type"] == gr and g["skin"] == 0), None) \
                or next((g for g in grades if g["type"] == gr), None) or grades[0]
            gid = str(pick["id"])
            if gid not in tables:
                p2, _ = cached_prefab_file(cache, gid, variant)
                conds, has_nodes = [], False
                if p2:
                    try:
                        with open(p2) as fh:
                            jd = json.load(fh)
                        conds, has_nodes = jd.get("conditionals") or [], bool(jd.get("nodes"))
                    except Exception:
                        pass
                tables[gid] = (conds, has_nodes)
            conds, has_nodes = tables[gid]
            if conds:
                coll, parts = _combo_collection(context, gid, pick["path"], conds, int(ob.get("rust_model", 1)),
                                                has_nodes)
                need.update(parts)
            else:
                coll = prefab_collection(context, int(pick["id"]), {int(pick["id"]): pick["path"]})
            ob["rust_block_pid"] = bpid
            if ob.instance_collection != coll:
                ob.instance_collection = coll
                n += 1
            need.add(str(pick["id"]))
        if need:
            load_prefab_assets(context, cache, True, need, variant)
        return n

    def door_swings(context, cache=None, variant="lod0"):
        """Hinged doors: show the leaf open/closed from the rust_open property (driver), pivoting at the hinge."""
        cache = cache or _cache_dir()
        hinges, n = {}, 0
        for ob in prefab_instances(context.scene) + [o for o in context.scene.objects if o.get("rust_swing")]:
            if not ob.get("rust_uid") or ob.get("rust_swing") or ob.instance_collection is None:
                continue
            coll = ob.instance_collection
            if not is_door_path(coll.get("rust_prefab_path", "")):
                continue
            pid = _pid_of(coll)
            if pid not in hinges:
                p, v = cached_prefab_file(cache, pid, variant)
                hb = None
                if p:
                    with open(p) as fh:
                        nodes = json.load(fh)["nodes"]
                    leaf = next((nd for nd in nodes if "handle" not in nd["name"].lower()), None)
                    if leaf:
                        m = leaf["matrix"]
                        hb = mathutils.Vector((m[3], m[11], m[7]))  # Unity (x, y, z) translation -> Blender (x, z, y)
                hinges[pid] = hb
            hb = hinges[pid]
            if hb is None:
                continue
            sw_name = "RustDoorSwing_%s" % pid
            sw = bpy.data.collections.get(sw_name)
            if sw is None:
                sw = bpy.data.collections.new(sw_name)
                prefab_library(context).children.link(sw)
                e = bpy.data.objects.new("door_at_hinge", None)
                e.instance_type = "COLLECTION"
                e.instance_collection = coll
                e.location = -hb
                sw.objects.link(e)
            if "rust_open" not in ob.keys():
                ob["rust_open"] = int(bool(int(ob.get("rust_flags", 0)) & FLAG_OPEN))
            leaf = bpy.data.objects.new(ob.name + "_leaf", None)
            leaf["rust_helper"] = True
            leaf.instance_type = "COLLECTION"
            leaf.instance_collection = sw
            leaf.empty_display_size = 0.2
            for c in ob.users_collection:
                c.objects.link(leaf)
            leaf.parent = ob
            leaf.location = hb
            leaf.rotation_mode = "XYZ"
            dr = leaf.driver_add("rotation_euler", 2).driver
            dr.type = "SCRIPTED"
            var = dr.variables.new()
            var.name = "op"
            var.targets[0].id = ob
            var.targets[0].data_path = '["rust_open"]'
            dr.expression = "-op*%.6f" % math.radians(DOOR_OPEN_DEG)  # Unity +Y = Blender -Z (axis swap mirrors)
            for attr in ("hide_viewport", "hide_render"):  # follow the door's spawn/destroy visibility
                d2 = leaf.driver_add(attr).driver
                d2.type = "SCRIPTED"
                v2 = d2.variables.new()
                v2.name = "h"
                v2.targets[0].id = ob
                v2.targets[0].data_path = attr
                d2.expression = "h"
            ob.instance_type = "NONE"  # the leaf child draws the door now
            ob["rust_swing"] = True
            ob.empty_display_type = "PLAIN_AXES"
            ob.empty_display_size = 0.3
            n += 1
        return n

    class IMPORT_OT_rust_assets(bpy.types.Operator):
        """Replace placeholders in RustPrefab_* collections with meshes from the rust_extract.py cache"""
        bl_idname = "import_scene.rust_prefab_assets"
        bl_label = "Load Rust prefab assets"
        bl_options = {"REGISTER", "UNDO"}
        reload_all: BoolProperty(name="Reload already loaded", default=False)

        def execute(self, context):
            try:
                done, missing = load_prefab_assets(context, only_missing=not self.reload_all,
                                                   variant=_variant(context.scene.rust))
                apply_building_grades(context, variant=_variant(context.scene.rust))
                door_swings(context, variant=_variant(context.scene.rust))
            except RuntimeError as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            self.report({"INFO"}, "loaded %d prefabs, %d not in cache" % (done, missing))
            return {"FINISHED"}

    # ------------------------------------------------------------ "Rust" sidebar panel

    import subprocess
    import threading
    import queue
    import tempfile

    CAT_RADIUS = {"Monument": 120.0, "Dungeon": 40.0, "DungeonBase": 40.0}

    def _deps_dir():
        return os.path.join(bpy.utils.user_resource("CONFIG"), "rust_world_deps")

    def _deps_ok():
        d = _deps_dir()
        return os.path.isdir(os.path.join(d, "UnityPy"))

    def _variant(p):
        return variant_name("low" if p.lod == "LOW" else "lod0", p.hlod)

    def _inst_category(ob):
        return ob.get("rust_category") or ("Demo" if ob.get("rust_uid") else "Prefab")

    def _inst_radius(ob):
        r = CAT_RADIUS.get(_inst_category(ob), 8.0)
        return r * max(ob.matrix_world.to_scale())

    def _volumes(scene):
        return [o for o in scene.objects if o.get("rust_volume")]

    def _in_volume(vol, p, r):
        mw = vol.matrix_world
        try:
            lp = mw.inverted() @ p
        except ValueError:
            return False
        if vol.type == "EMPTY":
            lo = mathutils.Vector((-1, -1, -1)) * vol.empty_display_size
            hi = -lo
        else:
            bb = [mathutils.Vector(c) for c in vol.bound_box]
            lo = mathutils.Vector((min(c.x for c in bb), min(c.y for c in bb), min(c.z for c in bb)))
            hi = mathutils.Vector((max(c.x for c in bb), max(c.y for c in bb), max(c.z for c in bb)))
        sc = mw.to_scale()
        for k in range(3):  # sphere of radius r vs box (approx. in local units)
            rk = r / max(abs(sc[k]), 1e-6)
            if lp[k] < lo[k] - rk or lp[k] > hi[k] + rk:
                return False
        return True

    def _camera_kdtree(scene, step):
        cam = scene.camera
        if cam is None:
            return None
        from mathutils import kdtree
        cur = scene.frame_current
        pts = []
        for f in range(scene.frame_start, scene.frame_end + 1, max(1, step)):
            scene.frame_set(f)
            pts.append(cam.matrix_world.translation.copy())
        scene.frame_set(cur)
        kd = kdtree.KDTree(len(pts))
        for i, p in enumerate(pts):
            kd.insert(p, i)
        kd.balance()
        return kd

    def scope_instances(context):
        """(instances in scope, all instances) for the panel's current scope."""
        scene = context.scene
        p = scene.rust
        allin = prefab_instances(scene)
        if p.scope == "ALL":
            return allin, allin
        if p.scope == "LIST":
            want = {it.pid for it in p.items if it.checked}
            return [o for o in allin if _pid_of(o.instance_collection) in want], allin
        if p.scope == "SELECTED":
            return [o for o in allin if o.select_get()], allin
        if p.scope == "BOXES":
            vols = _volumes(scene)
            return [o for o in allin if any(_in_volume(v, o.matrix_world.translation, _inst_radius(o))
                                            for v in vols)], allin
        if p.scope == "CURSOR":
            c = scene.cursor.location
            return [o for o in allin if (o.matrix_world.translation - c).length < p.radius + _inst_radius(o)], allin
        if p.scope == "CAMERA":
            kd = _camera_kdtree(scene, p.cam_step)
            if kd is None:
                return [], allin
            return [o for o in allin if kd.find(o.matrix_world.translation)[2] < p.radius + _inst_radius(o)], allin
        return [], allin

    def refresh_list(context):
        p = context.scene.rust
        checked = {it.pid for it in p.items if it.checked}
        agg = {}
        for o in prefab_instances(context.scene):
            c = o.instance_collection
            pid = _pid_of(c)
            e = agg.setdefault(pid, [c, _inst_category(o), 0])
            e[2] += 1
        p.items.clear()
        for pid, (c, cat, n) in sorted(agg.items(), key=lambda kv: (kv[1][1], kv[1][0].get("rust_prefab_path", ""))):
            it = p.items.add()
            path = c.get("rust_prefab_path", "")
            it.pid = pid
            it.name = os.path.splitext(os.path.basename(path))[0] if path else pid
            it.path = path
            it.category = cat
            it.count = n
            it.checked = pid in checked
            v = c.get("rust_variant", "")
            it.status = {"": "-", "lod0": "LOD0", "low": "low", "lod0_hlod": "HLOD", "low_hlod": "HLOD"}.get(v, v)

    class RustPrefabItem(bpy.types.PropertyGroup):
        pid: StringProperty()
        path: StringProperty()
        category: StringProperty()
        count: IntProperty()
        checked: BoolProperty(name="Load")
        status: StringProperty()

    class RustSceneProps(bpy.types.PropertyGroup):
        scope: EnumProperty(name="Load", default="BOXES", items=[
            ("ALL", "Whole map", "Every placed prefab"),
            ("SELECTED", "Selected", "Prefab instances selected in the viewport"),
            ("BOXES", "Inside boxes", "Instances inside any load box (Add Box)"),
            ("CURSOR", "Around cursor", "Within a radius of the 3D cursor"),
            ("CAMERA", "Along camera path", "Within a radius of the scene camera over the frame range"),
            ("LIST", "From list", "Prefabs checked in the list below")])
        radius: FloatProperty(name="Radius", default=200.0, min=1.0, unit="LENGTH")
        cam_step: IntProperty(name="Sample every N frames", default=10, min=1)
        lod: EnumProperty(name="Detail", default="LOW", items=[
            ("LOD0", "LOD0 (detailed)", "Closest LOD"), ("LOW", "Far LOD (light)", "Last LOD - silhouettes")])
        hlod: BoolProperty(name="Monuments as one HLOD mesh", default=True,
                           description="Merged far-distance mesh per monument: very light, rough shape")
        textures: BoolProperty(name="Textures", default=False,
                               description="Off = only meshes + material colours (much faster); can be added later")
        max_tex: EnumProperty(name="Max size", default="1024", items=[(x, x, "") for x in ("256", "512", "1024", "2048")])
        hide_outside: BoolProperty(name="Hide instances outside", default=False,
                                   description="After loading, hide prefab instances that are outside the scope")
        items: bpy.props.CollectionProperty(type=RustPrefabItem)
        index: IntProperty()
        status: StringProperty()
        progress: FloatProperty(min=0, max=1)
        running: BoolProperty()
        cancel: BoolProperty()
        time_mode: EnumProperty(name="Time of day", default="KEEP", items=[
            ("KEEP", "As recorded", "Leave the recorded time of day"),
            ("CUSTOM", "Custom", "Use Hour below (key it to animate - dawn in 10 s etc.)")])
        hour: FloatProperty(name="Hour", default=12.0, min=0.0, max=24.0, precision=2,
                            description="Time of day written into the exported demo (animatable)")

    class RUST_UL_prefabs(bpy.types.UIList):
        def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
            row = layout.row(align=True)
            row.prop(item, "checked", text="")
            row.label(text=item.name)
            sub = row.row()
            sub.alignment = "RIGHT"
            sub.label(text="%s  x%d  %s" % (item.category, item.count, item.status))

        def filter_items(self, context, data, propname):
            items = getattr(data, propname)
            flt = [self.bitflag_filter_item] * len(items)
            if self.filter_name:
                f = self.filter_name.lower()
                flt = [self.bitflag_filter_item if (f in it.path.lower() or f in it.category.lower()) else 0
                       for it in items]
            return flt, []

    class RUST_OT_refresh_list(bpy.types.Operator):
        """Rebuild the prefab list from the imported map/demo"""
        bl_idname = "rust.refresh_list"
        bl_label = "Refresh list"

        def execute(self, context):
            refresh_list(context)
            return {"FINISHED"}

    class RUST_OT_list_check(bpy.types.Operator):
        """Check / uncheck prefabs in the list"""
        bl_idname = "rust.list_check"
        bl_label = "Check"
        mode: EnumProperty(items=[("ALL", "All", ""), ("NONE", "None", ""), ("INVERT", "Invert", ""),
                                  ("CATEGORY", "Category of active", ""), ("SELECTION", "From selection", ""),
                                  ("SCOPE", "From current scope", "")])

        def execute(self, context):
            p = context.scene.rust
            if not p.items:
                refresh_list(context)
            if self.mode == "SELECTION" or self.mode == "SCOPE":
                if self.mode == "SELECTION":
                    sel = {_pid_of(o.instance_collection) for o in context.selected_objects
                           if o.instance_type == "COLLECTION" and o.instance_collection
                           and o.instance_collection.name.startswith("RustPrefab_")}
                else:
                    old = p.scope
                    sel = {_pid_of(o.instance_collection) for o in scope_instances(context)[0]}
                    p.scope = old
                for it in p.items:
                    if it.pid in sel:
                        it.checked = True
                return {"FINISHED"}
            cat = p.items[p.index].category if (self.mode == "CATEGORY" and 0 <= p.index < len(p.items)) else None
            for it in p.items:
                if self.mode == "ALL":
                    it.checked = True
                elif self.mode == "NONE":
                    it.checked = False
                elif self.mode == "INVERT":
                    it.checked = not it.checked
                elif self.mode == "CATEGORY" and it.category == cat:
                    it.checked = True
            return {"FINISHED"}

    class RUST_OT_select_instances(bpy.types.Operator):
        """Select every placed instance of the checked prefabs (or of the current scope)"""
        bl_idname = "rust.select_instances"
        bl_label = "Select instances"
        use_scope: BoolProperty(default=False)

        def execute(self, context):
            if self.use_scope:
                obs = scope_instances(context)[0]
            else:
                want = {it.pid for it in context.scene.rust.items if it.checked}
                obs = [o for o in prefab_instances(context.scene) if _pid_of(o.instance_collection) in want]
            for o in context.selected_objects:
                o.select_set(False)
            n = 0
            for o in obs:
                if o.visible_get():
                    o.select_set(True)
                    n += 1
            self.report({"INFO"}, "%d instances" % n)
            return {"FINISHED"}

    class RUST_OT_add_box(bpy.types.Operator):
        """Add a load box at the 3D cursor; move / scale / rotate it, assets load inside (any mesh with
        the custom property rust_volume works too)"""
        bl_idname = "rust.add_box"
        bl_label = "Add load box"
        size: FloatProperty(name="Half size", default=50.0, min=1.0)

        def execute(self, context):
            coll = _collection("Rust Load Boxes", context.scene.collection)
            ob = bpy.data.objects.new("RustLoadBox", None)
            ob.empty_display_type = "CUBE"
            ob.empty_display_size = 1.0
            ob.scale = (self.size, self.size, self.size * 0.5)
            ob.location = context.scene.cursor.location
            ob["rust_volume"] = True
            ob.hide_render = True
            coll.objects.link(ob)
            for o in context.selected_objects:
                o.select_set(False)
            ob.select_set(True)
            context.view_layer.objects.active = ob
            context.scene.rust.scope = "BOXES"
            return {"FINISHED"}

    class RUST_OT_show_all(bpy.types.Operator):
        """Unhide every prefab instance"""
        bl_idname = "rust.show_all"
        bl_label = "Show all instances"

        def execute(self, context):
            for o in prefab_instances(context.scene):
                o.hide_viewport = False
            return {"FINISHED"}

    class _BackgroundJob:
        """Runs a command, streams its stdout lines into a queue."""

        def __init__(self, cmd, env=None):
            kw = {}
            if sys.platform == "win32":
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         bufsize=1, env=env, encoding="utf-8", errors="replace", **kw)
            self.q = queue.Queue()
            self.tail = []
            threading.Thread(target=self._pump, daemon=True).start()

        def _pump(self):
            for line in self.proc.stdout:
                self.q.put(line.rstrip())
            self.q.put(None)

    class RUST_OT_install_deps(bpy.types.Operator):
        """Install UnityPy + Pillow into a private folder used by the asset extractor (needs internet, ~1 min)"""
        bl_idname = "rust.install_deps"
        bl_label = "Install extractor dependencies"
        _job = None
        _timer = None

        def execute(self, context):
            py = sys.executable
            subprocess.call([py, "-m", "ensurepip", "--upgrade"])
            cmd = [py, "-m", "pip", "install", "--upgrade", "--target", _deps_dir(),
                   "UnityPy", "Pillow", "lz4", "attrs>=22"]
            self._job = _BackgroundJob(cmd)
            context.scene.rust.status = "installing UnityPy..."
            context.scene.rust.running = True
            self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
            context.window_manager.modal_handler_add(self)
            return {"RUNNING_MODAL"}

        def modal(self, context, event):
            if event.type != "TIMER":
                return {"PASS_THROUGH"}
            p = context.scene.rust
            while not self._job.q.empty():
                line = self._job.q.get()
                if line is None:
                    context.window_manager.event_timer_remove(self._timer)
                    p.running = False
                    ok = self._job.proc.wait() == 0 and _deps_ok()
                    p.status = "dependencies installed" if ok else "pip failed - see system console"
                    self.report({"INFO" if ok else "ERROR"}, p.status)
                    return {"FINISHED"}
                print(line)
                p.status = line[-60:]
            _redraw(context)
            return {"PASS_THROUGH"}

    def _redraw(context):
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()

    def extract_command(p, game, cache, idfile):
        cmd = [sys.executable, os.path.abspath(__file__), "extract", "--rust", game, "--out", cache,
               "--ids-file", idfile, "--lod", "low" if p.lod == "LOW" else "lod0"]
        if p.hlod:
            cmd.append("--hlod")
        if p.textures:
            cmd += ["--max-tex", p.max_tex]
        else:
            cmd.append("--no-tex")
        env = dict(os.environ)
        env["PYTHONPATH"] = _deps_dir() + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONNOUSERSITE"] = "1"
        return cmd, env

    class RUST_OT_load_assets(bpy.types.Operator):
        """Extract (in the background) and load meshes for the prefabs in the chosen scope"""
        bl_idname = "rust.load_assets"
        bl_label = "Load assets"
        _job = None
        _timer = None

        def execute(self, context):
            p = context.scene.rust
            if p.running:
                self.report({"WARNING"}, "already running")
                return {"CANCELLED"}
            inside, allin = scope_instances(context)
            if not inside:
                self.report({"WARNING"}, "nothing in scope")
                return {"CANCELLED"}
            self.inside = set(inside)
            self.all = allin
            self.ids = sorted({str(o.get("rust_block_pid") or _pid_of(o.instance_collection)) for o in inside})
            self.variant = _variant(p)
            self.cache = _cache_dir()
            # what is already cached in the wanted variant (+ textures if asked) can load right away
            game = _game_dir() or DEFAULT_GAME_DIR
            if not os.path.isfile(os.path.join(game, "Bundles", "AssetSceneManifest.json")):
                self.report({"ERROR"}, "Rust folder not set (add-on preferences)")
                return {"CANCELLED"}
            if not _deps_ok():
                self.report({"ERROR"}, "Install extractor dependencies first (button in the Rust panel)")
                return {"CANCELLED"}
            fd, self.idfile = tempfile.mkstemp(prefix="rust_ids_", suffix=".txt")
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(self.ids))
            self._job = _BackgroundJob(*extract_command(p, game, self.cache, self.idfile))
            p.running, p.cancel, p.progress = True, False, 0.0
            p.status = "extracting %d prefabs..." % len(self.ids)
            self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
            context.window_manager.modal_handler_add(self)
            return {"RUNNING_MODAL"}

        def _finish(self, context, ok):
            p = context.scene.rust
            context.window_manager.event_timer_remove(self._timer)
            try:
                os.remove(self.idfile)
            except OSError:
                pass
            p.running = False
            if not ok:
                p.status = "cancelled / failed - see system console"
                return {"CANCELLED"}
            p.status = "loading into Blender..."
            done, missing = load_prefab_assets(context, self.cache, True, set(self.ids), self.variant)
            graded = apply_building_grades(context, self.cache, self.variant)
            door_swings(context, self.cache, self.variant)
            if p.hide_outside and p.scope != "ALL":
                for o in self.all:
                    o.hide_viewport = o not in self.inside
            refresh_list(context)
            p.status = "loaded %d prefabs (%d without cache)%s" % (
                done, missing, ", %d building blocks graded" % graded if graded else "")
            p.progress = 1.0
            self.report({"INFO"}, p.status)
            return {"FINISHED"}

        def modal(self, context, event):
            if event.type != "TIMER":
                return {"PASS_THROUGH"}
            p = context.scene.rust
            if p.cancel:
                self._job.proc.terminate()
                return self._finish(context, False)
            while not self._job.q.empty():
                line = self._job.q.get()
                if line is None:
                    return self._finish(context, self._job.proc.wait() == 0)
                if line.startswith("PROGRESS"):
                    parts = line.split(" ", 4)
                    i, n = int(parts[1]), int(parts[2])
                    p.progress = i / max(n, 1)
                    p.status = "%d/%d %s" % (i, n, os.path.basename(parts[4].rsplit(" ", 1)[0]))
                elif line.startswith("TOTAL"):
                    p.status = "extracting %s prefabs..." % line.split()[1]
                else:
                    print("[rust extract]", line)
            _redraw(context)
            return {"PASS_THROUGH"}

    class RUST_OT_cancel(bpy.types.Operator):
        bl_idname = "rust.cancel"
        bl_label = "Cancel"

        def execute(self, context):
            context.scene.rust.cancel = True
            return {"FINISHED"}

    class VIEW3D_PT_rust(bpy.types.Panel):
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Rust"
        bl_label = "Rust assets"

        def draw(self, context):
            p = context.scene.rust
            L = self.layout
            col = L.column()
            col.prop(p, "scope")
            if p.scope in ("CURSOR", "CAMERA"):
                col.prop(p, "radius")
            if p.scope == "CAMERA":
                col.prop(p, "cam_step")
                if context.scene.camera is None:
                    col.label(text="no scene camera", icon="ERROR")
            if p.scope == "BOXES":
                r = col.row(align=True)
                r.operator("rust.add_box", icon="CUBE")
                r.label(text="%d box(es)" % len(_volumes(context.scene)))
            box = L.box()
            box.prop(p, "lod")
            box.prop(p, "hlod")
            r = box.row(align=True)
            r.prop(p, "textures")
            sub = r.row()
            sub.enabled = p.textures
            sub.prop(p, "max_tex", text="")
            box.prop(p, "hide_outside")
            if p.running:
                r = L.row(align=True)
                if hasattr(r, "progress"):
                    r.progress(factor=p.progress, type="BAR", text=p.status)
                else:
                    r.label(text=p.status)
                r.operator("rust.cancel", text="", icon="CANCEL")
            else:
                L.operator("rust.load_assets", icon="IMPORT")
                if p.status:
                    L.label(text=p.status)
            r = L.row(align=True)
            r.operator("rust.select_instances", text="Select in scope").use_scope = True
            r.operator("rust.show_all", text="Show all")
            if not _deps_ok():
                L.operator("rust.install_deps", icon="CONSOLE")

    class VIEW3D_PT_rust_list(bpy.types.Panel):
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Rust"
        bl_label = "Prefab list"
        bl_parent_id = "VIEW3D_PT_rust"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            p = context.scene.rust
            L = self.layout
            L.operator("rust.refresh_list", icon="FILE_REFRESH")
            L.template_list("RUST_UL_prefabs", "", p, "items", p, "index", rows=8)
            r = L.row(align=True)
            for mode, txt in (("ALL", "All"), ("NONE", "None"), ("INVERT", "Invert")):
                r.operator("rust.list_check", text=txt).mode = mode
            r = L.row(align=True)
            r.operator("rust.list_check", text="+ category").mode = "CATEGORY"
            r.operator("rust.list_check", text="+ selected").mode = "SELECTION"
            r = L.row(align=True)
            r.operator("rust.select_instances", text="Select checked")
            n = sum(1 for it in p.items if it.checked)
            r.label(text="%d checked" % n)
            if p.scope != "LIST":
                L.label(text='Set Load = "From list" to use the checks', icon="INFO")

    PANEL_CLASSES = (RustPrefabItem, RustSceneProps, RUST_UL_prefabs, RUST_OT_refresh_list, RUST_OT_list_check,
                     RUST_OT_select_instances, RUST_OT_add_box, RUST_OT_show_all, RUST_OT_install_deps,
                     RUST_OT_load_assets, RUST_OT_cancel, VIEW3D_PT_rust, VIEW3D_PT_rust_list)

    # ------------------------------------------------------------ demo edit / export

    FLAG_PROPS = (("rust_open", FLAG_OPEN), ("rust_on", FLAG_ON), ("rust_locked", FLAG_LOCKED))

    def _demo_roots(scene):
        return [c for c in bpy.data.collections if c.get("rust_demo_path")]

    def _blender_to_unity(mw):
        """Blender 4x4 (object) -> Unity (pos, euler deg). Inverse of unity_obj_to_blender."""
        loc = mw.to_translation()
        r = mw.to_3x3().normalized()
        m = [[r[0][0], r[0][2], r[0][1]], [r[2][0], r[2][2], r[2][1]], [r[1][0], r[1][2], r[1][1]]]  # S*R*S
        return (loc.x, loc.z, loc.y), mat_to_unity_euler(m)

    def _flag_value(ob, prop, frame):
        """Value of a flag property at a frame (animated or static); None if not set on the object."""
        if prop not in ob.keys():
            return None
        for fc in _fcurves(ob):
            if fc.data_path == '["%s"]' % prop:
                return bool(round(fc.evaluate(frame)))
        return bool(ob[prop])

    def _flag_frames(ob):
        fr = set()
        for fc in _fcurves(ob):
            if fc.data_path in ['["%s"]' % p for p, _ in FLAG_PROPS]:
                fr.update(kp.co[0] for kp in fc.keyframe_points)
        return sorted(fr)

    def _motion_range(ob):
        """Frame span of motion keys on the object or any of its parents (e.g. a hinge empty)."""
        fr, o = [], ob
        while o is not None:
            fr += [kp.co[0] for fc in _fcurves(o)
                   if fc.data_path in ("location", "rotation_quaternion", "rotation_euler", "scale")
                   for kp in fc.keyframe_points]
            o = o.parent
        return (min(fr), max(fr)) if fr else None

    def collect_demo_edits(context, root, crop=False, rate=30.0, do_motion=True, do_flags=True,
                           do_delete=True, do_clones=True):
        """Compare the Blender scene with the recording of `root` -> arguments for demo_edit()."""
        scene = context.scene
        path = root["rust_demo_path"]
        demo = read_demo(path)
        ents = {e["uid"]: e for e in demo["entities"]}
        raw = read_demo_raw(path)
        clock = demo_server_clock(raw)
        fps = scene.render.fps / scene.render.fps_base
        f0 = root.get("rust_frame0", scene.frame_start)
        start = root.get("rust_demo_start", 0.0)
        t_of = lambda fr: int(round((start + (fr - f0) / fps) * 1000))  # noqa: E731
        fr_of = lambda t: f0 + (t / 1000.0 - start) * fps  # noqa: E731
        recorder = int(demo["header"]["recorder"] or 0)
        keep_always = {u for u, e in ents.items() if e["userid"] and e["userid"] == recorder}

        objs = [o for o in scene.objects if o.get("rust_uid") and o.get("rust_demo") == path]
        originals = [o for o in objs if not o.get("rust_new") and o.name == o.get("rust_name")]
        clones = [o for o in scene.objects if o.get("rust_uid") and
                  (o.get("rust_new") or o.name != o.get("rust_name")) and o not in originals]
        by_uid = {int(o["rust_uid"]): o for o in originals}

        delete = set()
        if do_delete:
            imported = set(json.loads(root.get("rust_uids", "[]")))
            delete |= imported - set(by_uid)
        if crop:
            vols = _volumes(scene)
            if vols:
                for u, e in ents.items():
                    top = e
                    while top["parent"] and top["parent"] in ents:
                        top = ents[top["parent"]]
                    if not top["keys"]:
                        continue
                    p = top["keys"][0][1]
                    bp = mathutils.Vector((p[0], p[2], p[1]))
                    if not any(_in_volume(v, bp, 0.0) for v in vols):
                        delete.add(u)
        changed = True
        while changed:  # children go with their parent
            changed = False
            for u, e in ents.items():
                if u not in delete and e["parent"] in delete:
                    delete.add(u)
                    changed = True
        delete -= keep_always

        # hidden objects are not evaluated by the depsgraph: make everything involved visible while sampling
        involved = set(originals) | set(clones)
        for o in list(involved):
            p = o.parent
            while p is not None:
                involved.add(p)
                p = p.parent
        saved = []
        for o in involved:
            fcs = [fc for fc in _fcurves(o) if fc.data_path == "hide_viewport"]
            saved.append((o, o.hide_viewport, [(fc, fc.mute) for fc in fcs]))
            for fc in fcs:
                fc.mute = True
            o.hide_viewport = False
        context.view_layer.update()
        try:
            return _collect_edits_inner(context, root, scene, path, demo, ents, raw, clock, fps, f0, start, t_of, fr_of,
                                        originals, clones, by_uid, delete, rate, do_motion, do_flags, do_clones)
        finally:
            for o, hv, fcs in saved:
                for fc, m in fcs:
                    fc.mute = m
                o.hide_viewport = hv
            context.view_layer.update()

    def _collect_edits_inner(context, root, scene, path, demo, ents, raw, clock, fps, f0, start, t_of, fr_of,
                             originals, clones, by_uid, delete, rate, do_motion, do_flags, do_clones):
        def unity_pose(o):
            e = ents.get(int(o["rust_uid"])) if not (o.get("rust_new") or o in clones) else None
            par_uid = e["parent"] if e else 0
            mw = o.matrix_world
            if par_uid and par_uid in by_uid:  # demo-parented: pose is local to the parent entity
                mw = by_uid[par_uid].matrix_world.inverted() @ mw
            return _blender_to_unity(mw)

        # which objects changed
        edited = [o for o in originals if do_motion and int(o["rust_uid"]) not in delete
                  and "rust_sig" in o.keys() and o["rust_sig"] != _motion_sig(o)]
        animated = {o: _motion_range(o) for o in edited + (clones if do_clones else [])}
        # sample poses (one pass over time for everything)
        samples = {o: [] for o in animated}
        static_pose = {}
        cur = scene.frame_current
        times = set()
        for o, rg in animated.items():
            if rg is None:
                continue
            t0, t1 = t_of(rg[0]), t_of(rg[1])
            k = 0
            while t0 + k * 1000.0 / rate <= t1:
                times.add(int(t0 + k * 1000.0 / rate))
                k += 1
            times.add(t1)
        for t in sorted(times):
            fr = fr_of(t)
            scene.frame_set(int(math.floor(fr)), subframe=fr - math.floor(fr))
            for o, rg in animated.items():
                if rg is not None and t_of(rg[0]) <= t <= t_of(rg[1]):
                    samples[o].append((t,) + unity_pose(o))
        scene.frame_set(cur)
        for o, rg in animated.items():
            if rg is None:
                static_pose[o] = unity_pose(o)

        def pose_fn(o):
            sm = samples.get(o)
            if sm:
                ts = [x[0] for x in sm]
                import bisect

                def f(t):
                    k = min(max(bisect.bisect_left(ts, t), 0), len(sm) - 1)
                    return sm[k][1], sm[k][2]
                return f
            sp = static_pose.get(o)
            return (lambda t: sp) if sp else None

        motion, snapshots, flag_keys, freeze = {}, {}, {}, set()
        for o in edited:
            u = int(o["rust_uid"])
            if samples.get(o):
                motion[u] = [(t, p, e, clock(t)) for t, p, e in samples[o]]
            else:
                freeze.add(u)  # placed statically: drop all recorded positions
            snapshots.setdefault(u, {})["pose"] = pose_fn(o)

        def flags_at(o, base, frame):
            fl = base
            for prop, bit in FLAG_PROPS:
                v = _flag_value(o, prop, frame)
                if v is not None:
                    fl = (fl | bit) if v else (fl & ~bit)
            return fl

        if do_flags:
            for o in originals:
                u = int(o["rust_uid"])
                if u in delete or not any(p in o.keys() for p, _ in FLAG_PROPS):
                    continue
                if o.get("rust_flag_sig") == _flag_sig(o):
                    continue  # same as recorded
                base = int(o.get("rust_flags", 0))
                keys, prev = [], None
                for fr in [scene.frame_start] + _flag_frames(o):
                    fl = flags_at(o, base, fr)
                    if fl != prev:
                        keys.append((max(t_of(fr), 1), fl))
                        prev = fl
                flag_keys[u] = keys
                snapshots.setdefault(u, {})["flags"] = (lambda t, o=o, base=base: flags_at(o, base, fr_of(t)))
        for o in originals:  # grade changed on a building block
            u = int(o["rust_uid"])
            e = ents.get(u)
            if e is not None and e.get("grade") is not None and "rust_grade" in o.keys() \
                    and int(o["rust_grade"]) != int(e["grade"]) and u not in delete:
                snapshots.setdefault(u, {})["grade"] = int(o["rust_grade"])

        clone_list = []
        if do_clones and clones:
            donors = {}
            next_uid = max(list(ents) + [0]) + 100000
            positions = [(mathutils.Vector((e["keys"][0][1][0], e["keys"][0][1][2], e["keys"][0][1][1])), e["group"])
                         for e in ents.values() if e["keys"] and not e["parent"] and e.get("group")]
            t_spawn = max(t_of(scene.frame_start), 100)
            for o in clones:
                duid = int(o.get("rust_donor_uid") or o["rust_uid"])
                dpath = o.get("rust_demo") or path
                if dpath not in donors:
                    donors[dpath] = {}
                    for t, d, _ in read_demo_raw(dpath)["packets"]:
                        if d[0] == MSG_ENTITIES:
                            uu = entity_uid(d)
                            if uu not in donors[dpath]:
                                donors[dpath][uu] = d
                pk = donors[dpath].get(duid)
                if pk is None:
                    print("rust export: no donor snapshot for", o.name)
                    continue
                if "rust_uid_new" not in o.keys():
                    o["rust_uid_new"] = str(next_uid)
                    next_uid += 1
                nu = int(o["rust_uid_new"])
                sm = samples.get(o)
                pos, eul = (sm[0][1], sm[0][2]) if sm else static_pose.get(o) or _blender_to_unity(o.matrix_world)
                bp = o.matrix_world.translation
                group = min(positions, key=lambda pg: (pg[0] - bp).length)[1] if positions else None
                fl = flags_at(o, int(o.get("rust_flags", 0)), scene.frame_start) \
                    if any(p in o.keys() for p, _ in FLAG_PROPS) else None
                clone_list.append({"packet": pk, "uid": nu, "group": group, "pos": pos, "euler": eul, "t_ms": t_spawn,
                                   "flags": fl, "grade": int(o["rust_grade"]) if "rust_grade" in o.keys() else None})
                if sm:
                    motion[nu] = [(max(t, t_spawn + 1), p, e, clock(max(t, t_spawn + 1))) for t, p, e in sm]
                if do_flags and fl is not None:
                    keys, prev = [], fl
                    for fr in _flag_frames(o):
                        f2 = flags_at(o, int(o.get("rust_flags", 0)), fr)
                        if f2 != prev:
                            keys.append((max(t_of(fr), t_spawn + 1), f2))
                            prev = f2
                    if keys:
                        flag_keys[nu] = keys
        return raw, dict(delete=delete, motion=motion, flag_keys=flag_keys, snapshots=snapshots, freeze=freeze,
                         clones=clone_list, t_range=(t_of(scene.frame_start), t_of(scene.frame_end)))

    def recorded_hour(root, t_s):
        env = json.loads(root.get("rust_env", "[]"))
        if not env:
            return None
        import bisect
        k = bisect.bisect_right([e[0] for e in env], t_s) - 1
        return env[max(k, 0)][1]

    def time_function(context, root):
        """hour_at(t_ms) from scene.rust.hour (static or keyed), animated flag."""
        scene = context.scene
        fps = scene.render.fps / scene.render.fps_base
        f0 = root.get("rust_frame0", scene.frame_start)
        start = root.get("rust_demo_start", 0.0)
        fc = next((f for f in _fcurves(scene) if f.data_path == "rust.hour"), None)
        if fc is None:
            h = scene.rust.hour
            return (lambda t: h), False
        return (lambda t: fc.evaluate(f0 + (t / 1000.0 - start) * fps)), True

    def export_demo(context, root, out_path, **opts):
        time_mode = opts.pop("time_mode", None) or context.scene.rust.time_mode
        raw, edits = collect_demo_edits(context, root, **opts)
        stats = demo_edit(raw, **edits)
        stats["time"] = (0, 0)
        if time_mode == "CUSTOM":
            fn, animated = time_function(context, root)
            stats["time"] = demo_set_time(raw, fn, every_ms=500 if animated else 0)
        n = write_demo_raw(raw, out_path)
        stats.update(bytes=n, deleted=len(edits["delete"]), moved=len(edits["motion"]),
                     flagged=len(edits["flag_keys"]), cloned=len(edits["clones"]))
        return stats

    def _root_items(self, context):
        items = [(c.name, c.name, c["rust_demo_path"]) for c in _demo_roots(context.scene)]
        return items or [("", "(no imported demo)", "")]

    class EXPORT_OT_rust_demo(bpy.types.Operator, ExportHelper):
        """Write a new demo = the imported recording + your edits (moves, animation, flags, deletes, clones)"""
        bl_idname = "export_scene.rust_demo"
        bl_label = "Export Rust demo"
        filename_ext = ".dem"
        filter_glob: StringProperty(default="*.dem", options={"HIDDEN"})
        source: EnumProperty(name="Recording", items=_root_items)
        motion: BoolProperty(name="Moved / animated entities", default=True)
        flags: BoolProperty(name="Open / On / Locked keys", default=True)
        deletes: BoolProperty(name="Deleted entities", default=True)
        clones: BoolProperty(name="New (cloned) entities", default=True)
        crop: BoolProperty(name="Keep only inside load boxes", default=False)
        rate: FloatProperty(name="Position rate (Hz)", default=30.0, min=5.0, max=60.0)

        def invoke(self, context, event):
            roots = _demo_roots(context.scene)
            if roots and not self.filepath:
                src = roots[0]["rust_demo_path"]
                self.filepath = os.path.splitext(src)[0] + "_edit.dem"
            return ExportHelper.invoke(self, context, event)

        def execute(self, context):
            root = bpy.data.collections.get(self.source)
            if root is None or not root.get("rust_demo_path"):
                self.report({"ERROR"}, "import a demo first")
                return {"CANCELLED"}
            if os.path.abspath(self.filepath) == os.path.abspath(root["rust_demo_path"]):
                self.report({"ERROR"}, "choose a new file name - the recording is never overwritten")
                return {"CANCELLED"}
            st = export_demo(context, root, self.filepath, crop=self.crop, rate=self.rate, do_motion=self.motion,
                             do_flags=self.flags, do_delete=self.deletes, do_clones=self.clones)
            self.report({"INFO"}, "%s: moved %d, flags %d, deleted %d, new %d, time %s (+%d packets, -%d)" % (
                os.path.basename(self.filepath), st["moved"], st["flagged"], st["deleted"], st["cloned"],
                "custom" if st["time"][0] else "as recorded", st["added"] + st["time"][1], st["dropped"]))
            return {"FINISHED"}

    class RUST_OT_clone_entity(bpy.types.Operator):
        """Duplicate selected demo entities as NEW entities for export (keeps the original in place)"""
        bl_idname = "rust.clone_entity"
        bl_label = "Clone entity"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            made = []
            for o in [o for o in context.selected_objects if o.get("rust_uid")]:
                n = o.copy()
                n.animation_data_clear()
                n.hide_viewport = n.hide_render = False
                for k in ("rust_sig", "rust_uid_new"):
                    if k in n.keys():
                        del n[k]
                n["rust_new"] = True
                n["rust_donor_uid"] = o.get("rust_donor_uid") or o["rust_uid"]
                n.parent = None
                n.matrix_world = o.matrix_world.copy()
                n.location.x += 1.0
                for c in o.users_collection:
                    c.objects.link(n)
                for ch in [c for c in o.children if c.get("rust_helper")]:  # door leaf: copy + retarget drivers
                    nc = ch.copy()
                    for c in ch.users_collection:
                        c.objects.link(nc)
                    nc.parent = n
                    nc.matrix_parent_inverse = ch.matrix_parent_inverse.copy()
                    if nc.animation_data:
                        for fc in nc.animation_data.drivers:
                            for var in fc.driver.variables:
                                for tg in var.targets:
                                    if tg.id == o:
                                        tg.id = n
                made.append(n)
            for o in context.selected_objects:
                o.select_set(False)
            for n in made:
                n.select_set(True)
            if made:
                context.view_layer.objects.active = made[-1]
            self.report({"INFO"}, "%d new entities" % len(made))
            return {"FINISHED"}

    class RUST_OT_key_flag(bpy.types.Operator):
        """Toggle a flag (Open / On / Locked) on the selected entities at the current frame and key it"""
        bl_idname = "rust.key_flag"
        bl_label = "Key flag"
        bl_options = {"REGISTER", "UNDO"}
        flag: EnumProperty(items=[("rust_open", "Open", ""), ("rust_on", "On", ""), ("rust_locked", "Locked", "")])

        def execute(self, context):
            bit = dict(FLAG_PROPS)[self.flag]
            fr = context.scene.frame_current
            n = 0
            for o in context.selected_objects:
                if not o.get("rust_uid"):
                    continue
                cur = _flag_value(o, self.flag, fr)
                if cur is None:
                    cur = bool(int(o.get("rust_flags", 0)) & bit)
                    o[self.flag] = int(cur)
                keyed = any(fc.data_path == '["%s"]' % self.flag for fc in _fcurves(o))
                if not keyed and fr != context.scene.frame_start:  # hold the current state up to this frame
                    o[self.flag] = int(cur)
                    o.keyframe_insert('["%s"]' % self.flag, frame=context.scene.frame_start)
                o[self.flag] = int(not cur)
                o.keyframe_insert('["%s"]' % self.flag, frame=fr)
                for fc in _fcurves(o):
                    if fc.data_path == '["%s"]' % self.flag:
                        for kp in fc.keyframe_points:
                            kp.interpolation = "CONSTANT"
                n += 1
            self.report({"INFO"}, "%s keyed on %d entities" % (self.flag[5:], n))
            return {"FINISHED"}

    class RUST_OT_refresh_grades(bpy.types.Operator):
        """Re-apply building grades after editing rust_grade on blocks"""
        bl_idname = "rust.refresh_grades"
        bl_label = "Refresh building grades"

        def execute(self, context):
            n = apply_building_grades(context, variant=_variant(context.scene.rust))
            self.report({"INFO"}, "%d blocks updated" % n)
            return {"FINISHED"}

    class RUST_OT_key_hour(bpy.types.Operator):
        """Key the time of day at the current frame (Custom mode)"""
        bl_idname = "rust.key_hour"
        bl_label = "Key hour"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            sc_ = context.scene
            sc_.rust.time_mode = "CUSTOM"
            sc_.keyframe_insert("rust.hour", frame=sc_.frame_current)
            return {"FINISHED"}

    class VIEW3D_PT_rust_demo_edit(bpy.types.Panel):
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Rust"
        bl_label = "Demo edit / export"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            L = self.layout
            o = context.active_object
            if o is not None and o.get("rust_uid"):
                b = L.box()
                b.label(text="%s  uid %s%s" % (o.name, o.get("rust_uid_new") or o["rust_uid"],
                                               "  (new)" if o.get("rust_new") or o.name != o.get("rust_name") else ""))
                if "rust_grade" in o.keys():
                    b.prop(o, '["rust_grade"]', text="Grade (0 twig .. 4 armored)")
                fl = int(o.get("rust_flags", 0))
                b.label(text="flags %d%s%s" % (fl, "  open" if fl & FLAG_OPEN else "", "  on" if fl & FLAG_ON else ""))
            r = L.row(align=True)
            r.operator("rust.key_flag", text="Key Open").flag = "rust_open"
            r.operator("rust.key_flag", text="Key On").flag = "rust_on"
            r.operator("rust.key_flag", text="Key Lock").flag = "rust_locked"
            r = L.row(align=True)
            r.operator("rust.clone_entity", icon="DUPLICATE")
            r.operator("rust.refresh_grades", text="Grades", icon="FILE_REFRESH")
            p = context.scene.rust
            b = L.box()
            b.prop(p, "time_mode")
            roots = _demo_roots(context.scene)
            if roots:
                sc_ = context.scene
                fps = sc_.render.fps / sc_.render.fps_base
                r0 = roots[0]
                t_s = r0.get("rust_demo_start", 0.0) + (sc_.frame_current - r0.get("rust_frame0", sc_.frame_start)) / fps
                rh = recorded_hour(r0, t_s)
                if rh is not None:
                    b.label(text="recorded here: %02d:%02d" % (int(rh), int(round((rh % 1) * 60)) % 60), icon="TIME")
            if p.time_mode == "CUSTOM":
                r = b.row(align=True)
                r.prop(p, "hour")
                r.operator("rust.key_hour", text="", icon="KEY_HLT")
                b.label(text="%02d:%02d" % (int(p.hour) % 24, int(round((p.hour % 1) * 60)) % 60))
            L.operator("export_scene.rust_demo", text="Export demo...", icon="EXPORT")

    EXPORT_CLASSES = (EXPORT_OT_rust_demo, RUST_OT_clone_entity, RUST_OT_key_flag, RUST_OT_refresh_grades, RUST_OT_key_hour,
                      VIEW3D_PT_rust_demo_edit)

    def _menu_export(self, context):
        self.layout.operator(EXPORT_OT_rust_demo.bl_idname, text="Rust demo (.dem)")

    def _menu(self, context):
        self.layout.operator(IMPORT_OT_rust_map.bl_idname, text="Rust map (.map)")
        self.layout.operator(IMPORT_OT_rust_demo.bl_idname, text="Rust demo (.dem)")
        self.layout.operator(IMPORT_OT_rust_assets.bl_idname, text="Rust prefab assets (cache)")

    class RustWorldPrefs(bpy.types.AddonPreferences):
        bl_idname = __name__
        game_dir: StringProperty(name="Rust folder", subtype="DIR_PATH", default=DEFAULT_GAME_DIR,
                                 description="Folder with Bundles/AssetSceneManifest.json (prefab names)")
        cache_dir: StringProperty(name="Asset cache", subtype="DIR_PATH", default="",
                                  description="rust_extract.py output; empty = <Rust folder>/rust_blender_cache")

        def draw(self, context):
            self.layout.prop(self, "game_dir")
            self.layout.prop(self, "cache_dir")

    def register():
        bpy.utils.register_class(RustWorldPrefs)
        bpy.utils.register_class(IMPORT_OT_rust_map)
        bpy.utils.register_class(IMPORT_OT_rust_demo)
        bpy.utils.register_class(IMPORT_OT_rust_assets)
        for c in PANEL_CLASSES + EXPORT_CLASSES:
            bpy.utils.register_class(c)
        bpy.types.Scene.rust = bpy.props.PointerProperty(type=RustSceneProps)
        bpy.types.TOPBAR_MT_file_import.append(_menu)
        bpy.types.TOPBAR_MT_file_export.append(_menu_export)

    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(_menu)
        bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
        del bpy.types.Scene.rust
        for c in reversed(PANEL_CLASSES + EXPORT_CLASSES):
            bpy.utils.unregister_class(c)
        bpy.utils.unregister_class(IMPORT_OT_rust_assets)
        bpy.utils.unregister_class(IMPORT_OT_rust_demo)
        bpy.utils.unregister_class(IMPORT_OT_rust_map)
        bpy.utils.unregister_class(RustWorldPrefs)


# ---------------------------------------------------------------- CLI


def _main(argv):
    from collections import Counter
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "mapinfo":
        w = read_map(argv[2])
        print("version", w["version"], "size", w["size"])
        for k, v in w["maps"].items():
            print("  layer %-9s %d bytes" % (k, len(v)))
        print("prefabs", len(w["prefabs"]), Counter(p["category"] for p in w["prefabs"]).most_common())
        print("distinct prefab ids", len({p["id"] for p in w["prefabs"]}))
        print("paths", [(p["name"], len(p["nodes"])) for p in w["paths"]])
    elif cmd == "prefabnames":
        src = argv[2]
        if os.path.isdir(src):
            src = os.path.join(src, "Bundles", "AssetSceneManifest.json")
        names = names_from_manifest(src)
        with open(argv[3], "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in sorted(names.items())}, fh, indent=0)
        print(len(names), "prefab names ->", argv[3])
    elif cmd == "demoroundtrip":
        for p in argv[2:]:
            data = open(p, "rb").read()
            raw = read_demo_raw(p)
            again = demo_bytes(raw)
            print(("OK   " if again == data else "DIFF ") + p, len(raw["packets"]), "packets", len(data), len(again))
    elif cmd == "demotime":
        # demotime in.dem out.dem 17:30            fixed time
        # demotime in.dem out.dem 6:00-20:00 30    ramp over N seconds from the demo start, then hold
        src, dst, spec = argv[2], argv[3], argv[4]
        hm = lambda x: int(x.split(":")[0]) + (int(x.split(":")[1]) if ":" in x else 0) / 60.0  # noqa: E731
        raw = read_demo_raw(src)
        if "-" in spec:
            a_, b_ = (hm(x) for x in spec.split("-"))
            dur = float(argv[5]) * 1000 if len(argv) > 5 else 30000.0
            if b_ < a_:
                b_ += 24
            fn = lambda t: a_ + (b_ - a_) * min(max(t / dur, 0.0), 1.0)  # noqa: E731
            r = demo_set_time(raw, fn, every_ms=500)
        else:
            h = hm(spec)
            r = demo_set_time(raw, lambda t: h)
        write_demo_raw(raw, dst)
        print("environment snapshots rewritten %d, added %d -> %s" % (r[0], r[1], dst))
    elif cmd in ("demoinfo", "demojson"):
        d = read_demo(argv[2])
        if cmd == "demojson":
            with open(argv[3], "w") as fh:
                json.dump(d, fh)
            return 0
        print(d["header"])
        es = d["entities"]
        mv = [e for e in es if len(e["keys"]) > 1]
        print("entities", len(es), "moving", len(mv), "parented", sum(1 for e in es if e["parent"]),
              "players", [(e["name"], e["userid"]) for e in es if e["name"]])
        print("own ticks", len(d["ticks"]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "extract":
        sys.exit(extract_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ("mapinfo", "demoinfo", "demojson", "prefabnames", "demoroundtrip", "demotime"):
        sys.exit(_main(sys.argv))
    elif HAVE_BPY:
        register()
    else:
        print(__doc__)
