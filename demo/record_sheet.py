"""実演用に、架空の注文の生産指示記録票を作る。右上の QR は 20mm 角。

A4 に A5 の票を2枚ずつ並べる。中身は demo/data/orders.json の架空の注文。
    uv run --with qrcode --with reportlab python demo/record_sheet.py
"""

import json
from datetime import date
from pathlib import Path

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent
OUT = HERE / "record-sheets.pdf"
QR_SIZE = 20 * mm
GOTHIC = "HeiseiKakuGo-W5"
MINCHO = "HeiseiMin-W3"
W, H = A4[0], A4[1] / 2  # A5 横長
M = 10 * mm
PROCESSES = ["裁断", "プレス", "仕上げ", "内職検査", "検査", "包装"]


def draw_qr(c: canvas.Canvas, text: str, x: float, y: float) -> None:
    """点を直接描き、黒い部分をちょうど QR_SIZE 角にする。"""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    dot = QR_SIZE / len(matrix)
    c.setFillGray(0)
    for r, row in enumerate(matrix):
        for col, black in enumerate(row):
            if black:
                c.rect(x + col * dot, y + QR_SIZE - (r + 1) * dot, dot, dot, stroke=0, fill=1)


def grid(c, x, top, widths, rows, height, header=False):
    """表を描く。rows は文字列のリストのリスト。見出し行は薄く塗る。"""
    y = top
    for i, row in enumerate(rows):
        cx = x
        if header and i == 0:
            c.setFillGray(0.92)
            c.rect(x, y - height, sum(widths), height, stroke=0, fill=1)
        c.setFillGray(0)
        for w, text in zip(widths, row):
            c.rect(cx, y - height, w, height, stroke=1, fill=0)
            if text:
                c.setFont(GOTHIC, 8 if header and i == 0 else 9.5)
                c.drawString(cx + 1.5 * mm, y - height + (height - 3 * mm) / 2, text)
            cx += w
        y -= height
    return y


def label_value(c, x, top, label_w, value_w, pairs, height):
    """「項目｜値」を横に並べる。項目の欄は薄く塗る。"""
    cx = x
    for label, value in pairs:
        c.setFillGray(0.92)
        c.rect(cx, top - height, label_w, height, stroke=0, fill=1)
        c.setFillGray(0)
        c.rect(cx, top - height, label_w, height, stroke=1, fill=0)
        c.rect(cx + label_w, top - height, value_w, height, stroke=1, fill=0)
        c.setFont(GOTHIC, 8)
        c.drawString(cx + 1.5 * mm, top - height + (height - 2.6 * mm) / 2, label)
        c.setFont(GOTHIC, 11)
        c.drawString(cx + label_w + 2 * mm, top - height + (height - 3.6 * mm) / 2, value)
        cx += label_w + value_w
    return top - height


def sheet(c: canvas.Canvas, order: dict, i: int, oy: float, issued: date) -> None:
    top = oy + H - M
    right = W - M
    c.setLineWidth(0.6)
    c.setStrokeGray(0)

    # 右上の QR と、その下の注文番号
    qx = right - QR_SIZE
    qy = top - QR_SIZE
    draw_qr(c, order["order_no"], qx, qy)
    c.setFont(GOTHIC, 8)
    c.drawCentredString(qx + QR_SIZE / 2, qy - 3.5 * mm, order["order_no"])

    c.setFont(MINCHO, 18)
    c.drawString(M, top - 7 * mm, "生産指示記録票")
    c.setFont(GOTHIC, 8)
    c.drawString(M, top - 12 * mm, f"発行日 {issued:%Y/%m/%d}　　No. DEMO-{i + 1:04d}")
    c.setFillGray(0.4)
    c.drawString(M, top - 16 * mm, "デモ用の架空の帳票です")
    c.setFillGray(0)

    table_w = qx - 4 * mm - M
    y = top - 19 * mm
    y = label_value(c, M, y, 16 * mm, table_w / 2 - 16 * mm,
                    [("注文No", order["order_no"]), ("品番", order["item_code"])], 9 * mm)
    y = label_value(c, M, y, 16 * mm, table_w / 2 - 16 * mm,
                    [("得意先", order["customer"]), ("納期", (order["due_date"] or "-").replace("-", "/"))], 9 * mm)
    full = right - M
    y = label_value(c, M, y - 2 * mm, 16 * mm, full / 4 - 16 * mm, [
        ("注文数", f"{order['qty']:,}"),
        ("材料", f"DEMO-M{(i % 4) + 1}"),
        ("金型", f"K-{101 + i}"),
        ("取数", f"{(4, 6, 8, 12)[i % 4]}"),
    ], 8 * mm)

    # 分納番号はエレベーター前の画面に出た番号を手で書く
    y -= 3 * mm
    c.setLineWidth(1.2)
    c.rect(M, y - 11 * mm, 46 * mm, 11 * mm, stroke=1, fill=0)
    c.setLineWidth(0.6)
    c.setFont(GOTHIC, 9)
    c.drawString(M + 2 * mm, y - 7 * mm, "分納")
    c.setFont(GOTHIC, 7.5)
    c.setFillGray(0.35)
    c.drawString(M + 50 * mm, y - 5 * mm, "エレベーター前でカードをかざすと、画面に分納番号が出ます。")
    c.drawString(M + 50 * mm, y - 9 * mm, "その番号をこの欄に書いてください。")
    c.setFillGray(0)
    y -= 15 * mm

    widths = [22 * mm, 22 * mm, 22 * mm, 26 * mm, 22 * mm]
    widths.append(full - sum(widths))
    rows = [["工程", "日付", "担当", "良品数", "不良数", "備考"]] + [[p, "", "", "", "", ""] for p in PROCESSES]
    y = grid(c, M, y, widths, rows, 6.2 * mm, header=True)

    # 確認印
    stamp = 18 * mm
    sx = right - 3 * stamp
    grid(c, sx, y - 4 * mm, [stamp] * 3, [["作成", "検査", "承認"]], 5 * mm, header=True)
    for k in range(3):
        c.rect(sx + k * stamp, y - 9 * mm - stamp, stamp, stamp, stroke=1, fill=0)


def main() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont(GOTHIC))
    pdfmetrics.registerFont(UnicodeCIDFont(MINCHO))
    orders = json.loads((HERE / "data" / "orders.json").read_text(encoding="utf-8"))
    c = canvas.Canvas(str(OUT), pagesize=A4)
    issued = date.today()
    for i, order in enumerate(orders):
        upper = i % 2 == 0
        sheet(c, order, i, H if upper else 0, issued)
        if upper:
            # 切り取り線
            c.setDash(3, 3)
            c.setStrokeGray(0.6)
            c.line(0, H, W, H)
            c.setDash()
            c.setStrokeGray(0)
        else:
            c.showPage()
    if len(orders) % 2:
        c.showPage()
    c.save()
    print(OUT)


if __name__ == "__main__":
    main()
