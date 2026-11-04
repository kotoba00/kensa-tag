"""読み取り機の筐体を生成する。寸法の出典は DIMENSIONS.md。

座標（組み立て状態、単位 mm）:
    x: 左右。中心が 0
    y: 前後。QR Base＋AtomS3R の後端（USB-C 側）が 0、前（レンズ側）が +
    z: 上下。QR Base の底面が 0、上（画面側）が +

本体の真下に RFID2 を表を下にして置き、底面でカードを読む。
部品は tray（本体）と cap（後ろのふた）の2つ。どちらの電装も後ろから滑り込ませる。
"""

from pathlib import Path

import cadquery as cq

# --- 部品の寸法 ---------------------------------------------------------
UNIT_W = 24.0  # QR Base＋AtomS3R の幅
UNIT_L = 48.0  # 同 長さ
UNIT_H = 21.0  # 同 高さ（AtomS3R の上面まで）
QR_H = 17.5  # QR Base の上面。AtomS3R がここから 3.5 飛び出す（実測）
ATOM_L = 24.0  # AtomS3R の前後長（y=0 から）
SCREEN_MARGIN = 3.4  # AtomS3R の外形から画面までの縁

USB_Z = 16.0  # USB-C の中心の高さ（実測）
USB_SHELL = (8.4, 2.6, 6.5)  # 金属部の幅、厚さ、長さ
USB_PLUG = (11.0, 7.5, 25.0)  # プラグの幅（実測）、厚さ、長さ（厚さと長さは仮）
CABLE_D = 5.0
GROVE_ATOM_Z = 10.0  # AtomS3R の Grove の中心の高さ（実測）
GROVE_PLUG = (9.6, 4.8, 3.0)  # Grove プラグの幅、高さ、差し込み口から出る長さ（長さは実測）
GROVE_CABLE_LEN = 200.0

RFID_W = 24.0
RFID_L = 48.0
RFID_H = 8.0
RFID_ANTENNA_FROM_GROVE = 32.0  # シールの中心。アンテナ中心の推定

RESET_Y = 16.4  # AtomS3R のリセット（左側面）の中心（実測）
RESET_Z = 14.0
LENS_Y = 45.0
LENS_Z = 10.3
FOV_H = 34.0
FOV_V = 28.0

# --- 筐体の寸法 ---------------------------------------------------------
FIT = 0.2  # はめ合いのすき間（片側）
WALL = 2.4  # 側壁。スナップの逃げのため 2 枚分の厚み
TOP = 1.6  # 画面の周りの天板
RFID_WALL = 1.2  # カードと RFID2 の間の壁
RFID_GAP = 2.0  # RFID2 の裏と本体の底の間。受けレールの高さに使う
RAIL = 1.7  # 本体を受けるレールの出幅
CHAMBER = 16.4  # 後ろの配線室の奥行き
BACK_WALL = 1.6
FRONT_WALL = 1.6
LIP_H = 2.5  # 前の抜け止め（QR Base の下唇より低い）
PLUG_CLEAR = 0.45  # USB プラグの周りのすき間
CORNER_R = 2.5

INNER_X = UNIT_W / 2 + FIT
OUTER_X = INNER_X + WALL
SLEEVE_INNER_TOP = UNIT_H + 2 * FIT
SLEEVE_TOP = SLEEVE_INNER_TOP + TOP
QR_INNER_TOP = QR_H + 2 * FIT
QR_TOP = QR_INNER_TOP + TOP
STEP_Y = ATOM_L + FIT  # 天板の段の内側。AtomS3R の前の面に沿う
STEP_OUTER_Y = STEP_Y + TOP * (2**0.5 - 1)  # 外の45度の始まり。段の角でも天板の厚みを残す
RFID_TOP = -RFID_GAP  # RFID2 の裏（上を向く面）
FLOOR_TOP = RFID_TOP - RFID_H - FIT  # RFID2 の表が載る面
BOTTOM = FLOOR_TOP - RFID_WALL
RFID_FACE = FLOOR_TOP
FRONT_OUTER = UNIT_L + FRONT_WALL
REAR_INNER = -CHAMBER
REAR_OUTER = REAR_INNER - BACK_WALL
ANTENNA_Y = RFID_ANTENNA_FROM_GROVE

USB_HOLE_HALF = USB_PLUG[0] / 2 + PLUG_CLEAR + 0.15
USB_HOLE_Z = (USB_Z - USB_PLUG[1] / 2 - PLUG_CLEAR, USB_Z + USB_PLUG[1] / 2 + PLUG_CLEAR)
SPLIT_Z = USB_HOLE_Z[0]  # tray の配線室の壁の上端＝cap の壁の下端
CAP_CEILING = SLEEVE_INNER_TOP
CAP_TOP = SLEEVE_TOP

HOOK_Y = (-9.0,)
HOOK_HALF = 6.0
HOOK_THICK = 1.3
INNER_WALL = 1.0  # フックの内側に残す壁
WINDOW_Z = (1.5, 3.5)
LEG_X = (8.0, 12.0)  # 本体と RFID2 の抜け止めの足。プラグの横を通す
LEG_Y = (-2.3, -FIT / 2)
LEG_RECESS_Z = 15.0  # ここより上の足は後ろへ下げる。AtomS3R の後ろの金色のコイルをよける
LEG_RECESS_Y = -1.2


def box(x0, x1, y0, y1, z0, z1):
    return (
        cq.Workplane("XY")
        .box(x1 - x0, y1 - y0, z1 - z0, centered=False)
        .translate((x0, y0, z0))
    )


def yz_prism(points, half_width):
    """YZ 平面の多角形を x 方向に押し出す。"""
    return cq.Workplane("YZ").polyline(points).close().extrude(half_width, both=True)


def xz_prism(points, y0, y1):
    """XZ 平面の多角形を y0〜y1 に押し出す。"""
    return (
        cq.Workplane("XZ", origin=(0, y1, 0))
        .polyline(points)
        .close()
        .extrude(y1 - y0)
    )


def z_cylinder(x, y, d, z0, z1):
    return cq.Workplane("XY", origin=(x, y, z0)).circle(d / 2).extrude(z1 - z0)


def x_cylinder(y, z, d, x0, x1):
    return cq.Workplane("YZ", origin=(x0, y, z)).circle(d / 2).extrude(x1 - x0)


def segment(p0, p1, d):
    """p0 から p1 への丸棒。配線の通り道の確認に使う。"""
    a, b = cq.Vector(*p0), cq.Vector(*p1)
    solid = cq.Solid.makeCylinder(d / 2, (b - a).Length, a, b - a)
    return cq.Workplane("XY").add(solid)


def plan_outline(z0, z1):
    return (
        box(-OUTER_X, OUTER_X, REAR_OUTER, FRONT_OUTER, z0, z1)
        .edges("|Z")
        .fillet(CORNER_R)
    )


def make_tray():
    body = plan_outline(BOTTOM, SLEEVE_TOP).intersect(
        yz_prism(
            [
                (REAR_INNER, BOTTOM - 1),
                (REAR_INNER, SPLIT_Z),
                (0, SPLIT_Z),
                (0, SLEEVE_TOP),
                (STEP_OUTER_Y, SLEEVE_TOP),
                (STEP_OUTER_Y + SLEEVE_TOP - QR_TOP, QR_TOP),
                (UNIT_L, QR_TOP),
                (UNIT_L, LIP_H),
                (FRONT_OUTER + 1, LIP_H),
                (FRONT_OUTER + 1, BOTTOM - 1),
            ],
            OUTER_X + 1,
        )
    )
    # 本体と RFID2 が入る筒。後ろの配線室はふたまで開けておく
    body = body.cut(box(-INNER_X, INNER_X, REAR_INNER - 1, STEP_Y, FLOOR_TOP, SLEEVE_INNER_TOP))
    body = body.cut(box(-INNER_X, INNER_X, STEP_Y - 0.01, UNIT_L, FLOOR_TOP, QR_INNER_TOP))
    body = body.cut(box(-INNER_X, INNER_X, UNIT_L - 0.01, UNIT_L + 1, LIP_H, QR_INNER_TOP))
    body = body.cut(box(-INNER_X, INNER_X, REAR_INNER - 1, 0, FLOOR_TOP, SPLIT_Z + 1))

    # 本体を受けるレール。下を45度にしてサポートをなくす
    for sign in (-1, 1):
        rail = xz_prism(
            [
                (sign * INNER_X, 0.0),
                (sign * (INNER_X - RAIL), 0.0),
                (sign * INNER_X, -RAIL),
            ],
            0.0,
            UNIT_L,
        )
        body = body.union(rail)

    # 画面の窓。外側を広げて斜めから見やすくする
    half = UNIT_W / 2 - SCREEN_MARGIN + 0.6
    window = (
        cq.Workplane("XY", origin=(0, ATOM_L / 2, SLEEVE_INNER_TOP - 0.01))
        .rect(2 * half, 2 * half)
        .workplane(offset=TOP + 0.02)
        .rect(2 * (half + TOP * 0.6), 2 * (half + TOP * 0.6))
        .loft(combine=True)
    )
    body = body.cut(window)

    # 通気: QR Base の通気穴の上とスキャナーの横
    for x in (-4.0, 0.0, 4.0):
        body = body.cut(box(x - 0.8, x + 0.8, 26.5, 31.0, QR_INNER_TOP - 1, SLEEVE_TOP + 1))
    for sign in (-1, 1):
        x0, x1 = sorted((sign * (INNER_X - 0.5), sign * (OUTER_X + 0.5)))
        for y in (32.0, 36.0, 40.0):
            body = body.cut(box(x0, x1, y - 0.8, y + 0.8, 7.0, 14.0))

    # リセットの押し穴（左側面）。丸穴の上を尖らせてサポートなしにする
    reset_d = 5.0
    r = reset_d / 2
    body = body.cut(x_cylinder(RESET_Y, RESET_Z, reset_d, -OUTER_X - 1, -INNER_X + 0.5))
    body = body.cut(
        cq.Workplane("YZ", origin=(-OUTER_X - 1, 0, 0))
        .polyline(
            [
                (RESET_Y - r * 0.7071, RESET_Z + r * 0.7071),
                (RESET_Y, RESET_Z + r * 1.4142),
                (RESET_Y + r * 0.7071, RESET_Z + r * 0.7071),
                (RESET_Y, RESET_Z),
            ]
        )
        .close()
        .extrude(WALL + 1.5)
    )

    # スナップ: 外側を削ってフックの逃げを作り、内壁に窓を開ける
    wall_outer = INNER_X + INNER_WALL
    for y in HOOK_Y:
        for sign in (-1, 1):
            x0, x1 = sorted((sign * wall_outer, sign * (OUTER_X + 1)))
            body = body.cut(box(x0, x1, y - HOOK_HALF - FIT, y + HOOK_HALF + FIT, 0.0, SPLIT_Z + 1))
            x0, x1 = sorted((sign * (INNER_X - 0.5), sign * (wall_outer + 0.5)))
            body = body.cut(box(x0, x1, y - HOOK_HALF + 1, y + HOOK_HALF - 1, *WINDOW_Z))

    return body.cut(card_mark())


def make_cap():
    cap = box(-OUTER_X, OUTER_X, REAR_OUTER, -FIT / 2, SPLIT_Z, CAP_TOP).union(
        box(-OUTER_X, OUTER_X, REAR_OUTER, REAR_INNER - FIT / 2, BOTTOM, SPLIT_Z + 0.01)
    )
    cap = cap.intersect(plan_outline(BOTTOM, CAP_TOP + 1))
    cap = cap.cut(box(-INNER_X, INNER_X, REAR_INNER, 1.0, SPLIT_Z - 1, CAP_CEILING))

    # USB プラグの通し穴。ふたを閉めたままケーブルを抜き差しできる
    cap = cap.cut(
        box(-USB_HOLE_HALF, USB_HOLE_HALF, REAR_OUTER - 1, REAR_INNER + 1, USB_HOLE_Z[0], USB_HOLE_Z[1])
    )

    # 本体と RFID2 が後ろへ抜けないよう押さえる足
    for sign in (-1, 1):
        x0, x1 = sorted((sign * LEG_X[0], sign * LEG_X[1]))
        cap = cap.union(box(x0, x1, LEG_Y[0], LEG_Y[1], FLOOR_TOP + FIT, LEG_RECESS_Z))
        cap = cap.union(box(x0, x1, LEG_Y[0], LEG_RECESS_Y, LEG_RECESS_Z - 0.01, CAP_CEILING + 0.01))
        x0, x1 = sorted((sign * LEG_X[1] - sign * 0.01, sign * INNER_X + sign * 0.01))
        cap = cap.union(box(x0, x1, LEG_Y[0], LEG_Y[1], SPLIT_Z, LEG_RECESS_Z))
        cap = cap.union(box(x0, x1, LEG_Y[0], LEG_RECESS_Y, LEG_RECESS_Z - 0.01, CAP_CEILING + 0.01))

    # スナップのフック。壁と同じ面で下へ伸び、爪が内壁の窓に入る
    hook_inner = OUTER_X - HOOK_THICK
    tip_x = hook_inner - 0.6
    for y in HOOK_Y:
        for sign in (-1, 1):
            x0, x1 = sorted((sign * hook_inner, sign * OUTER_X))
            cap = cap.union(box(x0, x1, y - HOOK_HALF, y + HOOK_HALF, 0.4, SPLIT_Z + 0.01))
            claw = xz_prism(
                [
                    (sign * hook_inner, WINDOW_Z[1] - 0.1),
                    (sign * tip_x, WINDOW_Z[1] - 0.1),
                    (sign * tip_x, WINDOW_Z[0] + 1.1),
                    (sign * hook_inner, WINDOW_Z[0] + 0.1),
                ],
                y - HOOK_HALF + 1 + FIT,
                y + HOOK_HALF - 1 - FIT,
            )
            cap = cap.union(claw)
            # フックをしならせやすくする切り込み
            sx0, sx1 = sorted((sign * hook_inner, sign * (OUTER_X + 1)))
            for dy in (-HOOK_HALF - 0.6, HOOK_HALF):
                cap = cap.cut(box(sx0, sx1, y + dy, y + dy + 0.6, SPLIT_Z - 0.01, SPLIT_Z + 4.0))
    return cap


def card_mark():
    """底面の刻印。カードを当てる場所を示す。"""
    depth = 0.4
    z0 = BOTTOM - 1
    z1 = BOTTOM + depth
    w, h, line = 22.0, 34.6, 0.8
    frame = (
        box(-w / 2, w / 2, ANTENNA_Y - h / 2, ANTENNA_Y + h / 2, z0, z1)
        .edges("|Z")
        .fillet(2.5)
        .cut(
            box(
                -w / 2 + line,
                w / 2 - line,
                ANTENNA_Y - h / 2 + line,
                ANTENNA_Y + h / 2 - line,
                z0 - 1,
                z1 + 1,
            )
            .edges("|Z")
            .fillet(1.7)
        )
    )
    center_y = ANTENNA_Y - 9.0
    wedge = (
        cq.Workplane("XY", origin=(0, 0, z0))
        .polyline([(0, center_y), (-20, center_y + 20), (20, center_y + 20)])
        .close()
        .extrude(z1 - z0)
    )
    mark = frame.union(z_cylinder(0, center_y, 2.6, z0, z1))
    for r in (6.0, 10.0, 14.0):
        ring = (
            cq.Workplane("XY", origin=(0, center_y, z0))
            .circle(r + line / 2)
            .circle(r - line / 2)
            .extrude(z1 - z0)
        )
        mark = mark.union(ring.intersect(wedge))
    return mark


def make_mocks():
    """確認と図のための部品の仮モデル。"""
    w, t, n = USB_SHELL
    pw, pt, pl = USB_PLUG
    gw, gh, gl = GROVE_PLUG
    plug_end = -n - pl
    return {
        "unit": yz_prism(
            [(0, 0), (0, UNIT_H), (ATOM_L, UNIT_H), (ATOM_L, QR_H), (UNIT_L, QR_H), (UNIT_L, 0)],
            UNIT_W / 2,
        ),
        "rfid2": box(-RFID_W / 2, RFID_W / 2, 0, RFID_L, RFID_FACE, RFID_FACE + RFID_H)
        .edges("|Z")
        .fillet(3.9),
        "usb_shell": box(-w / 2, w / 2, -n, 0, USB_Z - t / 2, USB_Z + t / 2),
        "usb_plug": box(-pw / 2, pw / 2, plug_end, -n, USB_Z - pt / 2, USB_Z + pt / 2),
        "usb_cable": segment((0, plug_end + 0.1, USB_Z), (0, plug_end - 20, USB_Z), CABLE_D),
        "grove_atom": box(-gw / 2, gw / 2, -gl, 0, GROVE_ATOM_Z - gh / 2, GROVE_ATOM_Z + gh / 2),
        "grove_rfid": box(-gw / 2, gw / 2, -gl, 0, RFID_FACE + 0.2, RFID_FACE + 0.2 + gh),
    }


def grove_route():
    """Grove ケーブルの通り道。2つのプラグの間に、余りを丸める輪を置く。

    (線分のリスト, 輪の1周の長さ) を返す。
    """
    d = 3.0
    y_plug = -GROVE_PLUG[2]
    z_rfid = RFID_FACE + 0.2 + GROVE_PLUG[1] / 2
    segments = [
        segment((0, y_plug, GROVE_ATOM_Z), (0, y_plug - 3.0, GROVE_ATOM_Z), d),
        segment((0, y_plug - 3.0, GROVE_ATOM_Z), (0, y_plug - 3.0, z_rfid), d),
        segment((0, y_plug - 3.0, z_rfid), (0, y_plug, z_rfid), d),
    ]
    # 余りの輪。線の束として太さ 6 mm で見る
    coil_d = 6.0
    y = REAR_INNER + coil_d / 2 + 0.3
    x = INNER_X - coil_d / 2 - 0.1
    z_low = FLOOR_TOP + coil_d / 2 + 0.1
    z_high = USB_HOLE_Z[0] - coil_d / 2 + 0.3
    corners = [(-x, z_low), (x, z_low), (x, z_high), (-x, z_high), (-x, z_low)]
    for (x0, z0), (x1, z1) in zip(corners, corners[1:]):
        segments.append(segment((x0, y, z0), (x1, y, z1), coil_d))
    loop_len = 2 * (2 * x + (z_high - z_low))
    return segments, loop_len


def fov_cone(length=120.0):
    """レンズの視野を四角すいにする。"""
    import math

    hx = math.tan(math.radians(FOV_H / 2)) * length
    hz = math.tan(math.radians(FOV_V / 2)) * length
    return (
        cq.Workplane("XZ", origin=(0, LENS_Y + 0.01, LENS_Z))
        .rect(0.02, 0.02)
        .workplane(offset=-length)
        .rect(2 * hx, 2 * hz)
        .loft(combine=True)
    )


def build():
    return {"tray": make_tray(), "cap": make_cap(), **make_mocks()}


def print_pose(name, shape):
    """印刷の向きへ。tray は底が、cap は天面がベッドに付く。"""
    if name == "tray":
        return shape.translate((0, 0, -BOTTOM))
    return shape.rotate((0, 0, 0), (0, 1, 0), 180).translate((0, 0, CAP_TOP))


def export_all(out: Path):
    parts = build()
    for name in ("tray", "cap"):
        posed = print_pose(name, parts[name])
        cq.exporters.export(posed, str(out / f"{name}.stl"), tolerance=0.02, angularTolerance=0.15)
        cq.exporters.export(parts[name], str(out / f"{name}.step"))

    colors = {
        "tray": (0.82, 0.84, 0.86),
        "cap": (0.25, 0.45, 0.70),
        "unit": (0.95, 0.65, 0.15),
        "rfid2": (0.95, 0.95, 0.95),
        "usb_shell": (0.6, 0.6, 0.6),
        "usb_plug": (0.15, 0.15, 0.15),
        "usb_cable": (0.15, 0.15, 0.15),
        "grove_atom": (0.9, 0.9, 0.85),
        "grove_rfid": (0.85, 0.2, 0.2),
    }
    assembly = cq.Assembly(name="reader")
    for name, color in colors.items():
        assembly.add(parts[name], name=name, color=cq.Color(*color))
    assembly.export(str(out / "assembly.step"))
    return parts


if __name__ == "__main__":
    here = Path(__file__).parent
    export_all(here)
    size = (2 * OUTER_X, FRONT_OUTER - REAR_OUTER, CAP_TOP - BOTTOM)
    print("外形 幅 %.1f × 長さ %.1f × 高さ %.1f mm" % size)
