# -*- coding: utf-8 -*-
"""
テキスト → 単線フォント → Gコード → ロボット手書き（Phase 2-a）
------------------------------------------------------------------
英数字を Hershey 単線フォントで線に変換し、ロボットで書く。
ペン制御は Phase 1 で確立した方式（Z軸・向き逆・G4同期）を使用。

使い方:
  python text2gcode.py "NEO"          # 文字高さ既定(10mm)で書く
  python text2gcode.py "Hello" 8      # 文字高さ8mmで書く
  python text2gcode.py "NEO" 10 --dry # 実機を動かさず範囲だけ確認

※ これは英数字のパイプライン確立用（Phase 2-a）。
   日本語（かな・漢字）対応は単線日本語フォント導入後（Phase 2-b）。
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

try:
    from HersheyFonts import HersheyFonts
except ImportError:
    print("Hershey-Fonts が入っていません: pip install Hershey-Fonts")
    sys.exit(1)

# ─────────────────────────────────────────────
# 設定（test_write.py と同じペン制御）
# ─────────────────────────────────────────────
SERIAL_PORT = 'COM3'
BAUD_RATE   = 115200

PEN_DOWN_Z = 10.0   # 紙に着く（向き逆：Z大で下がる）
PEN_UP_Z   = 0.0    # 紙から離れる
PEN_FEED   = 500    # ペン上下の速度
FEED       = 1000   # 描画速度

ORIGIN_X = 20       # 書き出し位置（紙の左下あたり）
ORIGIN_Y = 20

# 機械の可動域（はみ出しチェック用）
X_MAX = 210
Y_MAX = 297

DEFAULT_HEIGHT = 10.0  # 文字高さ mm


# ─────────────────────────────────────────────
# 通信まわり
# ─────────────────────────────────────────────
def send(ser, cmd):
    ser.write((cmd + '\n').encode())
    return ser.readline().decode(errors='ignore').strip()


def pen_up(ser):
    send(ser, f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.2')   # 同期


def pen_down(ser):
    send(ser, f'G1 Z{PEN_DOWN_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.2')   # 同期


def move_to(ser, x, y):
    send(ser, f'G0 X{x:.2f} Y{y:.2f}')


def line_to(ser, x, y):
    send(ser, f'G1 X{x:.2f} Y{y:.2f} F{FEED}')


# ─────────────────────────────────────────────
# テキスト → ストローク（連続した線のまとまり）
# ─────────────────────────────────────────────
def text_to_strokes(text, height):
    """
    Hershey フォントでテキストを線分化し、連続ストロークにまとめ、
    ロボット座標（左下原点・Y上向き）に変換して返す。
    返り値: [[(x,y), (x,y), ...], ...]  ストロークのリスト
    """
    font = HersheyFonts()
    font.load_default_font()
    font.normalize_rendering(height)   # 大文字の高さが height になる

    # Hershey は線分の列 ((x1,y1),(x2,y2)) を返す。
    # 前の終点と次の始点が一致するものを1ストロークにまとめる。
    raw = list(font.lines_for_text(text))
    if not raw:
        return []

    strokes = []
    current = []
    last_end = None
    for (x1, y1), (x2, y2) in raw:
        if last_end is None or abs(x1 - last_end[0]) > 1e-6 or abs(y1 - last_end[1]) > 1e-6:
            if current:
                strokes.append(current)
            current = [(x1, y1), (x2, y2)]
        else:
            current.append((x2, y2))
        last_end = (x2, y2)
    if current:
        strokes.append(current)

    # 全点から範囲を求め、左下原点・Y上向きに変換
    pts = [p for st in strokes for p in st]
    min_x = min(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)

    def conv(p):
        x = ORIGIN_X + (p[0] - min_x)
        y = ORIGIN_Y + (p[1] - min_y)   # Hershey は既にY上向きなので反転しない
        return (x, y)

    return [[conv(p) for p in st] for st in strokes]


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
        move_to(ser, *st[0])   # ペンを上げたまま始点へ
        pen_down(ser)
        for x, y in st[1:]:
            line_to(ser, x, y)
        pen_up(ser)
    move_to(ser, ORIGIN_X, ORIGIN_Y)


def main():
    args = [a for a in sys.argv[1:] if a != '--dry']
    dry = '--dry' in sys.argv

    if not args:
        print('使い方: python text2gcode.py "NEO" [文字高さmm] [--dry]')
        sys.exit(1)

    text = args[0]
    height = float(args[1]) if len(args) > 1 else DEFAULT_HEIGHT

    print(f'テキスト: "{text}"  文字高さ: {height}mm')
    strokes = text_to_strokes(text, height)
    if not strokes:
        print("描く線がありません（対応していない文字かも）。")
        sys.exit(1)

    x0, x1, y0, y1 = bounds(strokes)
    print(f'描画範囲: X[{x0:.1f}〜{x1:.1f}]  Y[{y0:.1f}〜{y1:.1f}]  '
          f'（ストローク数 {len(strokes)}）')

    if x1 > X_MAX or y1 > Y_MAX or x0 < 0 or y0 < 0:
        print("⚠️ 可動域からはみ出します。文字高さを小さくするか原点を調整してください。")
        sys.exit(1)

    if dry:
        print("--dry なので実機は動かしません。範囲チェックのみ。")
        return

    print(f"ポート {SERIAL_PORT} に接続して書き始めます...")
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        draw_strokes(ser, strokes)
        print("=== 書き出し完了 ===")
        print("紙を確認してください。")
    finally:
        ser.close()


if __name__ == '__main__':
    main()
