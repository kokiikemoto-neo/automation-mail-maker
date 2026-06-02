# -*- coding: utf-8 -*-
"""
日本語テキスト → フォント輪郭 → Gコード → ロボット手書き（Phase 2-b）
------------------------------------------------------------------
日本語フォント（TTF/TTC）のグリフ輪郭を freetype で取得し、
ベジェ曲線を多点展開してなめらかな線にしてロボットで書く。

フォントを差し替えるだけで字形（ゴシック・教科書体・手書き風・単線）を変えられる。
ペン制御は Phase 1 で確立した方式（Z軸・向き逆・G4同期）。

使い方:
  python jp_text2gcode.py "あ"            # 文字高さ既定(15mm)
  python jp_text2gcode.py "神奈川" 12      # 文字高さ12mm
  python jp_text2gcode.py "あ" 15 --dry    # 実機を動かさず範囲だけ確認

注意:
  輪郭フォントなので現状は「袋文字（輪郭線）」になる。
  手書き風にするには単線フォントへ差し替える（FONT_PATH を変更）。
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

try:
    import freetype
except ImportError:
    print("freetype-py が入っていません: pip install freetype-py")
    sys.exit(1)

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
SERIAL_PORT = 'COM3'
BAUD_RATE   = 115200

PEN_DOWN_Z = 10.0   # 紙に着く（向き逆：Z大で下がる）
PEN_UP_Z   = 0.0    # 紙から離れる
PEN_FEED   = 500
FEED       = 1000

ORIGIN_X = 20
ORIGIN_Y = 20
# はみ出しチェックの上限。紙サイズに合わせる（現在の紙: 190 × 230mm）。
# 原点(0,0)=紙の左下、X+=右、Y+=上 の前提。
X_MAX = 190
Y_MAX = 230

DEFAULT_HEIGHT = 15.0   # 文字高さ mm
BEZIER_STEPS   = 10     # ベジェ曲線の分割数（多いほどなめらか）

# 使用フォント（まずは Windows 標準の MS ゴシックで動作確認）
FONT_PATH  = r'C:\Windows\Fonts\msgothic.ttc'
FONT_INDEX = 0          # .ttc 内のフォント番号（0 = MS Gothic）


# ─────────────────────────────────────────────
# 通信まわり
# ─────────────────────────────────────────────
def send(ser, cmd):
    ser.write((cmd + '\n').encode())
    return ser.readline().decode(errors='ignore').strip()


def pen_up(ser):
    send(ser, f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.2')


def pen_down(ser):
    send(ser, f'G1 Z{PEN_DOWN_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.2')


def move_to(ser, x, y):
    send(ser, f'G0 X{x:.2f} Y{y:.2f}')


def line_to(ser, x, y):
    send(ser, f'G1 X{x:.2f} Y{y:.2f} F{FEED}')


# ─────────────────────────────────────────────
# グリフ輪郭 → ストローク（フォント単位の座標）
# ─────────────────────────────────────────────
def decompose_glyph(outline, steps=BEZIER_STEPS):
    """1グリフの輪郭を、閉じた輪郭ごとのストローク（点列）に分解する。"""
    strokes = []
    current = []

    def moveto(to, ctx):
        if current:
            strokes.append(list(current))
            current.clear()
        current.append((to.x, to.y))

    def lineto(to, ctx):
        current.append((to.x, to.y))

    def conicto(control, to, ctx):
        # 2次ベジェ（始点=current[-1], 制御=control, 終点=to）
        p0 = current[-1]
        cx, cy = control.x, control.y
        px, py = to.x, to.y
        for i in range(1, steps + 1):
            t = i / steps
            mt = 1 - t
            x = mt * mt * p0[0] + 2 * mt * t * cx + t * t * px
            y = mt * mt * p0[1] + 2 * mt * t * cy + t * t * py
            current.append((x, y))

    def cubicto(c1, c2, to, ctx):
        # 3次ベジェ
        p0 = current[-1]
        ax, ay = c1.x, c1.y
        bx, by = c2.x, c2.y
        px, py = to.x, to.y
        for i in range(1, steps + 1):
            t = i / steps
            mt = 1 - t
            x = mt**3 * p0[0] + 3 * mt**2 * t * ax + 3 * mt * t * t * bx + t**3 * px
            y = mt**3 * p0[1] + 3 * mt**2 * t * ay + 3 * mt * t * t * by + t**3 * py
            current.append((x, y))

    outline.decompose(move_to=moveto, line_to=lineto,
                       conic_to=conicto, cubic_to=cubicto)
    if current:
        strokes.append(list(current))
    return strokes


def text_to_strokes(text, height, fontpath, font_index):
    """日本語テキストをロボット座標のストローク列に変換する。"""
    face = freetype.Face(fontpath, font_index)
    upm = face.units_per_EM          # フォントの基準サイズ（例 1000 や 2048）
    scale = height / upm             # フォント単位 → mm

    raw = []
    pen_x = 0                        # 文字送り（フォント単位）
    for ch in text:
        face.load_char(ch, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_BITMAP)
        for st in decompose_glyph(face.glyph.outline):
            raw.append([(pen_x + x, y) for (x, y) in st])
        pen_x += face.glyph.advance.x

    if not raw:
        return []

    pts = [p for st in raw for p in st]
    min_x = min(p[0] for p in pts)
    min_y = min(p[1] for p in pts)

    def conv(p):
        # フォント座標は Y 上向きなので反転不要。スケールして原点へ。
        return (ORIGIN_X + (p[0] - min_x) * scale,
                ORIGIN_Y + (p[1] - min_y) * scale)

    return [[conv(p) for p in st] for st in raw]


def bounds(strokes):
    pts = [p for st in strokes for p in st]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


# ─────────────────────────────────────────────
# 描画
# ─────────────────────────────────────────────
def draw_strokes(ser, strokes):
    send(ser, 'G21')
    send(ser, 'G90')
    pen_up(ser)
    for st in strokes:
        move_to(ser, *st[0])
        pen_down(ser)
        for x, y in st[1:]:
            line_to(ser, x, y)
        pen_up(ser)
    move_to(ser, ORIGIN_X, ORIGIN_Y)


def draw_frame(ser, strokes):
    """描画範囲の外接矩形を、ペンを上げたままなぞる（書く位置の事前確認用）。"""
    x0, x1, y0, y1 = bounds(strokes)
    send(ser, 'G21')
    send(ser, 'G90')
    pen_up(ser)
    for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]:
        move_to(ser, x, y)
        time.sleep(0.4)
    print(f'枠をなぞりました（ここに書かれます）: X[{x0:.1f}〜{x1:.1f}] Y[{y0:.1f}〜{y1:.1f}]')


def main():
    global ORIGIN_X, ORIGIN_Y, X_MAX, Y_MAX
    argv = list(sys.argv[1:])
    dry = '--dry' in argv
    frame = '--frame' in argv

    # --font でフォントを差し替え可能に
    fontpath = FONT_PATH
    font_index = FONT_INDEX
    if '--font' in argv:
        i = argv.index('--font')
        fontpath = argv[i + 1]
        font_index = 0          # 単体 .ttf は 0
        del argv[i:i + 2]

    # --origin X Y で書き出し位置（左下）を変更
    if '--origin' in argv:
        i = argv.index('--origin')
        ORIGIN_X = float(argv[i + 1])
        ORIGIN_Y = float(argv[i + 2])
        del argv[i:i + 3]

    # --paper W H で紙サイズ（はみ出し上限）を変更。紙は毎回変わるため指定する。
    if '--paper' in argv:
        i = argv.index('--paper')
        X_MAX = float(argv[i + 1])
        Y_MAX = float(argv[i + 2])
        del argv[i:i + 3]

    args = [a for a in argv if a not in ('--dry', '--frame')]

    if not args:
        print('使い方: python jp_text2gcode.py "文字" [高さmm] [--font パス] [--origin X Y] [--paper W H] [--frame] [--dry]')
        print('  --frame      書く前に描画範囲の枠をペンを上げたままなぞって位置確認')
        print('  --origin X Y 書き出し位置(左下)を変更（既定 20 20）')
        print('  --paper W H  紙サイズ(mm)を指定してはみ出しチェック（既定 190 230）')
        sys.exit(1)

    text = args[0]
    height = float(args[1]) if len(args) > 1 else DEFAULT_HEIGHT

    print(f'機械の可動域 : X[0〜{X_MAX}]  Y[0〜{Y_MAX}] (mm)')
    print(f'テキスト     : "{text}"  文字高さ {height}mm  原点 ({ORIGIN_X},{ORIGIN_Y})')
    print(f'フォント     : {fontpath}')
    strokes = text_to_strokes(text, height, fontpath, font_index)
    if not strokes:
        print("描く線がありません（フォントに無い文字かも）。")
        sys.exit(1)

    x0, x1, y0, y1 = bounds(strokes)
    print(f'描画範囲     : X[{x0:.1f}〜{x1:.1f}]  Y[{y0:.1f}〜{y1:.1f}]  '
          f'(幅{x1-x0:.1f} × 高{y1-y0:.1f}mm, ストローク {len(strokes)})')

    if x1 > X_MAX or y1 > Y_MAX or x0 < 0 or y0 < 0:
        print("⚠️ 可動域からはみ出します。文字高さを小さくするか --origin で位置調整を。")
        sys.exit(1)

    if dry:
        print("--dry なので実機は動かしません。範囲チェックのみ。")
        return

    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        if frame:
            print("--frame: 枠をなぞります（書きません）。紙のどこに書かれるか確認してください。")
            draw_frame(ser, strokes)
        else:
            print("書き始めます...")
            draw_strokes(ser, strokes)
            print("=== 書き出し完了 ===")
            print("紙を確認してください。")
    finally:
        ser.close()


if __name__ == '__main__':
    main()
