"""筐体を計算で確かめる。問題があれば終了コード 1 で終わる。

組み立ての干渉、部品を入れる経路、視野、通り道、壁厚、STL の閉じ方、張り出しを見る。
"""

import sys
from pathlib import Path

import numpy as np
import trimesh

import build_case as b

TOL = 1e-3
HERE = Path(__file__).parent
failures = []


def volume(shape):
    solids = shape.val() if hasattr(shape, "val") else shape
    return solids.Volume()


def overlap(a, c):
    try:
        return volume(a.intersect(c))
    except ValueError:
        return 0.0


def check(label, value, ok, unit=""):
    mark = "OK " if ok else "NG "
    print(f"{mark}{label}: {value:.3f}{unit}" if isinstance(value, float) else f"{mark}{label}: {value}")
    if not ok:
        failures.append(label)


parts = b.build()
tray, cap = parts["tray"], parts["cap"]
inside = ["unit", "rfid2", "usb_shell", "usb_plug", "usb_cable", "grove_atom", "grove_rfid"]

print("== 組み立てた状態の干渉 ==")
v = overlap(tray, cap)
check("tray と cap", v, v < TOL, " mm3")
for case_name, case in (("tray", tray), ("cap", cap)):
    for name in inside:
        v = overlap(case, parts[name])
        check(f"{case_name} と {name}", v, v < TOL, " mm3")
for x, y in [("unit", "rfid2"), ("usb_plug", "grove_atom"), ("grove_atom", "grove_rfid")]:
    v = overlap(parts[x], parts[y])
    check(f"{x} と {y}", v, v < TOL, " mm3")

print("== 入れる経路（後ろから滑らせる）==")
for name in ("unit", "rfid2"):
    worst = max(
        overlap(tray, parts[name].translate((0, float(dy), 0)))
        for dy in np.linspace(-b.UNIT_L - 10, 0, 15)
    )
    check(f"tray と {name}（経路の最大）", worst, worst < TOL, " mm3")

print("== cap を閉じる経路 ==")
hook_inner = b.OUTER_X - b.HOOK_THICK
tip_x = hook_inner - 0.6
wall_outer = b.INNER_X + b.INNER_WALL
cap_no_claw = cap
for sign in (-1, 1):
    x0, x1 = sorted((sign * (tip_x - 0.1), sign * hook_inner))
    cap_no_claw = cap_no_claw.cut(b.box(x0, x1, b.REAR_OUTER, 0, b.WINDOW_Z[0], b.WINDOW_Z[1]))
others = {"tray": tray, "unit": parts["unit"], "rfid2": parts["rfid2"], "grove_atom": parts["grove_atom"], "grove_rfid": parts["grove_rfid"]}
for name, shape in others.items():
    worst = max(
        overlap(shape, cap_no_claw.translate((0, 0, float(dz)))) for dz in np.linspace(0, 32, 9)
    )
    check(f"cap（爪以外）と {name}", worst, worst < TOL, " mm3")
check("爪のしなり量", float(wall_outer - tip_x), 0.2 <= wall_outer - tip_x <= 0.8, " mm")
check("爪の掛かり代", float(wall_outer - tip_x), wall_outer - tip_x >= 0.3, " mm")

print("== QR の視野 ==")
cone = b.fov_cone()
for case_name, case in (("tray", tray), ("cap", cap)):
    v = overlap(case, cone)
    check(f"視野と {case_name}", v, v < TOL, " mm3")

print("== 通り道 ==")
segments, loop_len = b.grove_route()
blockers = {
    "tray": tray,
    "cap": cap,
    "usb_plug": parts["usb_plug"],
    "usb_shell": parts["usb_shell"],
    "usb_cable": parts["usb_cable"],
}
for name, blocker in blockers.items():
    v = sum(overlap(blocker, seg) for seg in segments)
    check(f"Grove の線と {name}", v, v < TOL, " mm3")
turns = (b.GROVE_CABLE_LEN - 40) / loop_len
check("Grove の余りの巻き数（輪の1周 %.0f mm）" % loop_len, float(turns), turns <= 3.0, " 周")
hole = (2 * b.USB_HOLE_HALF, b.USB_HOLE_Z[1] - b.USB_HOLE_Z[0])
plug_ok = hole[0] > b.USB_PLUG[0] and hole[1] > b.USB_PLUG[1]
check("USB の通し穴", f"{hole[0]:.1f} × {hole[1]:.1f} mm（プラグ {b.USB_PLUG[0]} × {b.USB_PLUG[1]} mm）", plug_ok)

print("== 壁厚 ==")
b.export_all(HERE)
cap_mesh = trimesh.load(HERE / "cap.stl")
tray_mesh = trimesh.load(HERE / "tray.stl")


def first_wall(mesh, x, y):
    """印刷の向きの mesh を下から貫いて、最初の肉厚を測る。"""
    hits, _, _ = mesh.ray.intersects_location([[x, y, -1.0]], [[0, 0, 1.0]], multiple_hits=True)
    zs = sorted(set(np.round(hits[:, 2], 4)))
    return float(zs[1] - zs[0])


t = first_wall(tray_mesh, 3.0, 8.0)
check("カード面の壁厚（刻印の外）", t, abs(t - b.RFID_WALL) < 0.01, " mm")
t = first_wall(tray_mesh, 0.0, b.ANTENNA_Y - 9.0)
check("カード面の壁厚（刻印の上）", t, abs(t - (b.RFID_WALL - 0.4)) < 0.01, " mm")
face_gap = b.RFID_WALL
check("カードからアンテナ面までの壁", float(face_gap), face_gap <= 1.2, " mm")
check(
    "外形",
    f"{2 * b.OUTER_X:.1f} × {b.FRONT_OUTER - b.REAR_OUTER:.1f} × {b.CAP_TOP - b.BOTTOM:.1f} mm",
    True,
)

print("== STL ==")
meshes = {"tray": tray_mesh, "cap": cap_mesh}
for name, mesh in meshes.items():
    check(f"{name} が閉じた立体", bool(mesh.is_watertight), bool(mesh.is_watertight))
    check(f"{name} の体積が正", float(mesh.volume), mesh.volume > 0, " mm3")
    check(f"{name} がベッドに接する（最低 z）", float(mesh.bounds[0][2]), abs(mesh.bounds[0][2]) < 0.01, " mm")

print("== サポートなしで刷れるか（45度より寝た下向き面）==")
for name, mesh in meshes.items():
    normals = mesh.face_normals
    centers = mesh.triangles_center
    limit = -np.cos(np.radians(45.0)) - 0.01
    mask = (normals[:, 2] < limit) & (centers[:, 2] > 0.01)
    area = float(mesh.area_faces[mask].sum())
    print(f"   {name}: 下向き面の合計 {area:.0f} mm2")
    buckets = {}
    for c, a in zip(centers[mask], mesh.area_faces[mask]):
        key = round(float(c[2]), 1)
        buckets.setdefault(key, [[], 0.0])
        buckets[key][0].append(c)
        buckets[key][1] += float(a)
    for z, (cs, a) in sorted(buckets.items()):
        if a < 1.0:
            continue
        cs = np.array(cs)
        print(
            f"     z={z:5.1f}  面積{a:7.1f} mm2  x {cs[:, 0].min():6.1f}〜{cs[:, 0].max():6.1f}"
            f"  y {cs[:, 1].min():6.1f}〜{cs[:, 1].max():6.1f}"
        )

print()
if failures:
    print("NG:", ", ".join(failures))
    sys.exit(1)
print("すべて OK")
