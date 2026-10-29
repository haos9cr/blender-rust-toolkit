# Example: the first in-game test of demo editing (verified in game).
# Door uid 40787 toggles its Open flag (8-33 s), then swings 0-90-0 deg around its hinge
# via EntityPosition packets (40-70 s). Needs rust_world.py next to it or on PYTHONPATH.

"""Test: can a demo animate a door?
  A (8-32 s):  door toggles Open flag every 2 s           -> game's own open/close animation
  B (40-70 s): door swings 0..90..0 deg every 4 s via EntityPosition packets (whole entity, around its hinge)
usage: python make_door_test.py <in.dem> <out.dem> [uid]
"""
import math
import sys

import rust_world as rw

src, dst = sys.argv[1], sys.argv[2]
uid = int(sys.argv[3]) if len(sys.argv) > 3 else 40787
raw = rw.read_demo_raw(src)
demo = rw.read_demo(src)
e = next(x for x in demo["entities"] if x["uid"] == uid)
pos0, eul0 = e["keys"][0][1], e["keys"][0][2]
print("door", uid, "pos", [round(v, 2) for v in pos0], "euler", [round(v, 1) for v in eul0])
clock = rw.demo_server_clock(raw)
hinge = (0.016, 0.0, -0.598)  # door.hinged.metal: 'door' pivot in entity space


def rot_y(a, v):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return (c * v[0] + s * v[2], v[1], -s * v[0] + c * v[2])


R = rw.quat_to_mat(*rw.unity_euler_to_quat(*eul0))
mul = lambda m, v: tuple(sum(m[r][k] * v[k] for k in range(3)) for r in range(3))  # noqa: E731
new = []
# A: flags
for k, t in enumerate(range(8000, 32001, 2000)):
    new.append((t, rw.pk_entity_flags(uid, rw.FLAG_OPEN if k % 2 == 0 else 0)))
new.append((33000, rw.pk_entity_flags(uid, 0)))
# B: positions at 30 Hz
for k in range(0, 30 * 30 + 1):
    t = 40000 + k * 1000 / 30
    a = 90.0 * (1 - math.cos(2 * math.pi * ((t - 40000) / 4000.0))) / 2
    d = tuple(hc - qc for hc, qc in zip(hinge, rot_y(a, hinge)))  # keep the hinge in place
    off = mul(R, d)
    p = tuple(pc + oc for pc, oc in zip(pos0, off))
    eu = (eul0[0], (eul0[1] + a) % 360.0, eul0[2])
    new.append((int(t), rw.pk_entity_position(uid, p, eu, clock(t))))
rw.demo_insert(raw, new)
n = rw.write_demo_raw(raw, dst)
print("written", dst, n, "bytes,", len(new), "packets added")
