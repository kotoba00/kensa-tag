"""組み立て図、分解図、断面図、印刷の向きの画像を renders/ に出す。"""

from pathlib import Path

import numpy as np
import pyvista as pv
import trimesh

import build_case as b

pv.OFF_SCREEN = True
HERE = Path(__file__).parent
OUT = HERE / "renders"
OUT.mkdir(exist_ok=True)

COLORS = {
    "tray": "#d5d9de",
    "cap": "#3f73b3",
    "unit": "#f2a626",
    "rfid2": "#f4f4f4",
    "usb_shell": "#999999",
    "usb_plug": "#2a2a2a",
    "usb_cable": "#2a2a2a",
    "grove_atom": "#ecebd9",
    "grove_rfid": "#d43a3a",
    "screen": "#1b3a52",
}


def to_pv(shape, tol=0.05):
    vertices, faces = shape.val().tessellate(tol, 0.2)
    pts = np.array([v.toTuple() for v in vertices])
    return pv.PolyData.from_regular_faces(pts, np.array(faces))


def plotter(size=(1400, 900)):
    p = pv.Plotter(off_screen=True, window_size=size)
    p.set_background("white")
    return p


def add(p, parts, offsets=None, opacity=None):
    for name, shape in parts.items():
        mesh = to_pv(shape)
        if offsets and name in offsets:
            mesh = mesh.translate(offsets[name])
        p.add_mesh(
            mesh,
            color=COLORS[name],
            smooth_shading=False,
            specular=0.15,
            opacity=(opacity or {}).get(name, 1.0),
            feature_angle=30,
        )


def label(p, text):
    p.add_text(text, position="upper_left", font_size=11, color="black")


def view(p, direction, up=(0, 0, 1), zoom=1.15):
    d = np.array(direction, dtype=float)
    p.camera_position = [tuple(d * 500), (0, -10, 12), up]
    p.reset_camera()
    p.camera.zoom(zoom)


parts = b.build()
half = b.UNIT_W / 2 - b.SCREEN_MARGIN
parts["screen"] = b.box(
    -half, half, b.SCREEN_MARGIN, b.ATOM_L - b.SCREEN_MARGIN, b.UNIT_H - 0.02, b.UNIT_H + 0.02
)
closed = {k: v for k, v in parts.items()}

p = plotter()
add(p, closed)
label(p, "assembled: USB cable at near end, lens at far end")
view(p, (-0.55, -0.75, 0.6))
p.screenshot(OUT / "assembled_rear.png")
p.close()

p = plotter()
add(p, closed)
label(p, "assembled: scanner side")
view(p, (0.6, 0.75, 0.45))
p.screenshot(OUT / "assembled_front.png")
p.close()

p = plotter()
add(p, closed)
label(p, "bottom: hold the card against this mark")
view(p, (0.5, -0.5, -0.75), up=(0, 1, 0))
p.screenshot(OUT / "assembled_bottom.png")
p.close()

p = plotter()
add(
    p,
    closed,
    offsets={
        "cap": (0, -75, 12),
        "rfid2": (0, -75, -30),
        "grove_rfid": (0, -75, -30),
        "unit": (0, -75, 0),
        "screen": (0, -75, 0),
        "grove_atom": (0, -75, 0),
        "usb_shell": (0, -75, 0),
        "usb_plug": (0, -75, 0),
        "usb_cable": (0, -75, 0),
    },
)
label(p, "exploded: unit and RFID2 slide in from the rear, then the cap closes")
view(p, (-0.8, -0.45, 0.55), zoom=1.2)
p.screenshot(OUT / "exploded.png")
p.close()


def section(normal, origin, name, text):
    """normal の向きから見た断面。手前側を切り取る。"""
    pl = plotter()
    keep = tuple(-n for n in normal)
    for key, shape in closed.items():
        mesh = to_pv(shape).clean(tolerance=1e-4)
        clipped = mesh.clip_closed_surface(normal=keep, origin=origin)
        if clipped.n_cells:
            pl.add_mesh(clipped, color=COLORS[key], specular=0.1)
    label(pl, text)
    pl.enable_parallel_projection()
    d = np.array(normal, dtype=float)
    pl.camera_position = [tuple(np.array(origin) + d * 500), tuple(np.array(origin)), (0, 0, 1)]
    pl.reset_camera()
    pl.camera.zoom(1.25)
    pl.screenshot(OUT / name)
    pl.close()


section((1, 0, 0), (0, 0, 0), "section_center.png", "section x=0 (center)")
section((1, 0, 0), (10, 0, 0), "section_side.png", "section x=10 (rails, stop legs)")
section((0, 1, 0), (0, b.ANTENNA_Y, 0), "section_antenna.png", "section at antenna center")
section((0, 1, 0), (0, b.HOOK_Y[0], 0), "section_hook.png", "section at snap hook, cable chamber")
section((0, 1, 0), (0, b.RESET_Y, 0), "section_reset.png", "section at reset hole")

p = plotter()
for name, x_shift in (("tray", -22), ("cap", 22)):
    posed = b.print_pose(name, parts[name]).translate((x_shift, 0, 0))
    p.add_mesh(to_pv(posed), color=COLORS[name], specular=0.15)
plate = pv.Plane(center=(0, -10, -0.05), direction=(0, 0, 1), i_size=100, j_size=140)
p.add_mesh(plate, color="#eeeeee")
label(p, "print orientation: both flat on the bed")
p.camera_position = [(-180, -260, 190), (0, -10, 10), (0, 0, 1)]
p.reset_camera()
p.camera.zoom(1.2)
p.screenshot(OUT / "print_orientation.png")
p.close()
print("done")
