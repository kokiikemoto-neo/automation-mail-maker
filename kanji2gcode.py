# -*- coding: utf-8 -*-
"""
日本語（漢字・かな）→ KanjiVG 筆画 → Gコード → ロボット単線手書き（Phase 2-b）
------------------------------------------------------------------------------
KanjiVG の筆画ストロークデータ（文字の中心線）を使い、
「NEO」と同じ一本線（単線）で日本語を書く。輪郭フォントのような二重線にならない。
筆順どおりに書くので、より本物の手書きに近い。小さくしても潰れにくい。

筆画データ: fonts/kanjivg/ に各文字の SVG（ファイル名 = コードポイント5桁hex）。
  例: 池=06c60.svg 本=0672c.svg 光=05149.svg 輝=08f1d.svg

使い方:
  python kanji2gcode.py "池本光輝" 10
  python kanji2gcode.py "池本光輝" 10 --origin 20 20 --paper 190 230 --frame
  python kanji2gcode.py "池本光輝" 5  --dry

ペン制御は Phase 1 で確立した方式（Z軸・向き逆・G4同期）。
"""

import os
import sys
import time
import math
import random

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

try:
    from svgpathtools import svg2paths
except ImportError:
    print("svgpathtools が入っていません: pip install svgpathtools")
    sys.exit(1)

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
SERIAL_PORT = 'COM3'
BAUD_RATE   = 115200

PEN_DOWN_Z = 10.0
PEN_UP_Z   = 7.0    # 移動時のペン上げ位置。10で着く→7.0で約3mm浮く（擦らない最適値）
PEN_FEED   = 10000  # ペン上下速度（Z軸最大15000まで余裕あり）
FEED       = 6000   # 描画速度（XY最大15000）。速度優先で引き上げ

ORIGIN_X = 20
ORIGIN_Y = 20
X_MAX = 190        # 紙サイズ上限（--paper で変更）
Y_MAX = 230

DEFAULT_HEIGHT = 5.0    # 宛名の標準文字高さ（実機検証で 5mm がちょうど良いと確定）

KANJIVG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts', 'kanjivg')
KVG_EM      = 109.0    # KanjiVG の字面枠（109x109）
ADVANCE     = 109.0    # 1文字の送り幅（字間込みは下の係数で調整）
CHAR_GAP    = 1.10     # 文字送りの倍率（1.0でぴったり、>1で字間を空ける）
SAMPLE_STEP = 4.0      # 筆画を何 KanjiVG単位ごとに点を打つか（小さいほどなめらか）

# 手書き風ゆらぎ（単位は KanjiVG座標＝109が1文字分）。達筆すぎを和らげる。
HUMANIZE    = True     # 既定で手書き風ゆらぎを有効（--plain で無効化）
JIT_ROT_DEG = 1.0      # 文字ごとの傾きの揺れ（度）※プリセット1=ごく控えめで確定
JIT_SCALE   = 0.02     # 文字ごとの大きさの揺れ（±の割合）
JIT_POS     = 2.0      # 文字ごとの位置の揺れ
JIT_PT      = 0.0      # 線（各点）の震えは無し＝線はしっかり保つ


# ─────────────────────────────────────────────
# 通信まわり
# ─────────────────────────────────────────────
def send(ser, cmd, retries=2):
    """1行送って応答を返す。瞬断時はバッファをクリアして数回リトライ。"""
    for attempt in range(retries + 1):
        try:
            ser.write((cmd + '\n').encode())
            return ser.readline().decode(errors='ignore').strip()
        except serial.SerialException:
            if attempt < retries:
                time.sleep(0.5)
                try:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception:
                    pass
            else:
                raise


def pen_up(ser):
    send(ser, f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.03')


def pen_down(ser):
    send(ser, f'G1 Z{PEN_DOWN_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.03')


def move_to(ser, x, y):
    send(ser, f'G0 X{x:.2f} Y{y:.2f}')


def line_to(ser, x, y):
    send(ser, f'G1 X{x:.2f} Y{y:.2f} F{FEED}')


# ─────────────────────────────────────────────
# テキスト → 筆画ストローク
# ─────────────────────────────────────────────
def char_svg_path(ch):
    cp = format(ord(ch), '05x')
    return os.path.join(KANJIVG_DIR, cp + '.svg')


def text_to_strokes(text, height, vertical=False, humanize=HUMANIZE):
    """
    日本語テキストを KanjiVG 筆画からロボット座標のストローク列に変換。
    vertical=True で縦書き（上→下、改行で右→左の列）。
    横書きは左→右、改行で上→下の行。改行はテキスト中の \\n。
    humanize=True で文字ごとに傾き・大きさ・位置をばらつかせ、線も微震させる。
    """
    raw = []            # KanjiVG座標（Y下向き、文字ごとにオフセット）
    missing = []
    pitch = ADVANCE * CHAR_GAP
    half = KVG_EM / 2.0

    line = 0   # 改行カウント（横書き=行、縦書き=列）
    pos = 0    # 行/列内の文字位置
    for ch in text:
        if ch == '\n':
            line += 1
            pos = 0
            continue
        if ch.isspace() or ch == '　':
            pos += 1
            continue

        if vertical:
            ox = -line * pitch    # 列は右→左（後の列ほど左）
            oy = pos * pitch      # 上→下（KanjiVG は Y 下向き＝増で下）
        else:
            ox = pos * pitch      # 左→右
            oy = line * pitch     # 改行で下の行へ

        svgfile = char_svg_path(ch)
        if not os.path.exists(svgfile):
            missing.append(ch)
            pos += 1
            continue

        # この文字のゆらぎ（傾き・大きさ・位置を1文字につき1セット決める）
        if humanize:
            rot = math.radians(random.uniform(-JIT_ROT_DEG, JIT_ROT_DEG))
            sc = 1.0 + random.uniform(-JIT_SCALE, JIT_SCALE)
            cdx = random.uniform(-JIT_POS, JIT_POS)
            cdy = random.uniform(-JIT_POS, JIT_POS)
            cos_r, sin_r = math.cos(rot), math.sin(rot)

        paths, _ = svg2paths(svgfile)
        for path in paths:
            length = path.length()
            n = max(4, int(length / SAMPLE_STEP))
            pts = []
            for i in range(n + 1):
                z = path.point(i / n)
                lx, ly = z.real, z.imag
                if humanize:
                    # 字面中心まわりに微小回転＋スケール、位置ずらし、点の震え
                    ux, uy = lx - half, ly - half
                    rx = half + (ux * cos_r - uy * sin_r) * sc
                    ry = half + (ux * sin_r + uy * cos_r) * sc
                    px = ox + rx + cdx + random.uniform(-JIT_PT, JIT_PT)
                    py = oy + ry + cdy + random.uniform(-JIT_PT, JIT_PT)
                else:
                    px, py = ox + lx, oy + ly
                pts.append((px, py))
            raw.append(pts)
        pos += 1

    if not raw:
        return [], missing

    scale = height / KVG_EM
    pts_all = [p for st in raw for p in st]
    min_x = min(p[0] for p in pts_all)
    min_y = min(p[1] for p in pts_all)
    max_y = max(p[1] for p in pts_all)   # KanjiVG は Y 下向きなので反転に使う

    def conv(p):
        x = ORIGIN_X + (p[0] - min_x) * scale
        y = ORIGIN_Y + (max_y - p[1]) * scale   # Y反転（下向き→上向き）
        return (x, y)

    return [[conv(p) for p in st] for st in raw], missing


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


# ─────────────────────────────────────────────
# 描画（新方式：GRBL character-counting ストリーミング）
#   speed_test.py で検証・確定した設定を移植。旧 draw_strokes は温存。
#   batch_write.py の USE_STREAMING / --stream で切替（既定は旧方式）。
# ─────────────────────────────────────────────
RX_BUFFER_SIZE    = 128     # GRBL 受信バッファ（character-counting の上限）
STREAM_DWELL_UP   = 0.05    # ペン上げ後の待ち秒（実機検証で渡り線が消えた確定値）
STREAM_DWELL_DOWN = 0.03    # ペン下げ後の待ち秒（確定値）


def _stream_drain(ser, c_line, where):
    """未回収の ok/error を全て回収して c_line を空にする。
    error: は即中断、応答途絶（timeout連発）もガードで中断。"""
    empty = 0
    while c_line:
        resp = ser.readline().decode(errors='ignore').strip()
        if resp == '':
            empty += 1
            if empty > 3:
                raise RuntimeError(f'GRBL 応答が途絶（{where}）')
            continue
        empty = 0
        if resp == 'ok':
            c_line.pop(0)
        elif resp.startswith('error'):
            c_line.pop(0)
            raise RuntimeError(f'GRBL {resp}（{where}）')
        # 情報行 '[...]' '<...>' は無視


def draw_strokes_streamed(ser, strokes,
                          dwell_up=STREAM_DWELL_UP, dwell_down=STREAM_DWELL_DOWN):
    """draw_strokes と同じ筆順・命令で書くが、character-counting で連続送信する。
    1点ごとの ok 待ちをやめ、GRBL の先読みでまとめて流す（高速・滑らか）。
    安全策：error: 検出で即中断／終端フラッシュで全 ok 回収／応答途絶ガード。
    ペン上下後は G4 dwell でペンが物理的に動ききるのを待つ（渡り線対策）。"""
    # 送る命令列を組み立て（draw_strokes と同一の流れ。dwell だけ可変）
    prog = ['G21', 'G90',
            f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}', f'G4 P{dwell_up:.3f}']   # 初期ペン上げ
    for st in strokes:
        x0, y0 = st[0]
        prog.append(f'G0 X{x0:.2f} Y{y0:.2f}')                  # move_to
        prog.append(f'G1 Z{PEN_DOWN_Z:.2f} F{PEN_FEED}')       # pen_down
        prog.append(f'G4 P{dwell_down:.3f}')                   # 下げ切り待ち
        for x, y in st[1:]:
            prog.append(f'G1 X{x:.2f} Y{y:.2f} F{FEED}')       # line_to
        prog.append(f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}')         # pen_up
        prog.append(f'G4 P{dwell_up:.3f}')                     # 上げ切り待ち（渡り線対策）
    prog.append(f'G0 X{ORIGIN_X:.2f} Y{ORIGIN_Y:.2f}')         # 原点へ戻る

    # character-counting ストリーミング
    c_line = []
    ser.reset_input_buffer()
    empty = 0
    for i, line in enumerate(prog):
        c_line.append(len(line) + 1)   # +1 は改行ぶん
        # 受信バッファに空きが要る、または応答が来ている間は回収
        while sum(c_line) >= RX_BUFFER_SIZE - 1 or ser.in_waiting:
            resp = ser.readline().decode(errors='ignore').strip()
            if resp == '':
                empty += 1
                if empty > 3:
                    raise RuntimeError(f'GRBL 応答が途絶（送信中・行{i}）')
                continue
            empty = 0
            if resp == 'ok':
                c_line.pop(0)
            elif resp.startswith('error'):
                c_line.pop(0)
                # エラーを握りつぶさず即中断
                raise RuntimeError(f'GRBL {resp} 付近 行{i}: {line!r}')
            # 情報行 '[...]' '<...>' は無視
        ser.write((line + '\n').encode())
    _stream_drain(ser, c_line, 'flush')   # 終端フラッシュ（未回収の ok を全部待つ）


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


def open_serial():
    """GRBL をリセットせずに接続（DTR/RTS を立てない）。座標を保ったまま再接続できる。"""
    s = serial.Serial()
    s.port = SERIAL_PORT
    s.baudrate = BAUD_RATE
    s.timeout = 5
    s.dtr = False
    s.rts = False
    s.open()
    return s


def draw_chunked(strokes, chunk=40):
    """ストロークを小分けにし、チャンクごとに接続し直して書く。
    USB が途中で切れても、そのチャンクを再接続してやり直すだけで済む（座標は保持）。"""
    i = 0
    n = len(strokes)
    while i < n:
        sub = strokes[i:i + chunk]
        for attempt in range(6):
            ser = None
            try:
                ser = open_serial()
                time.sleep(0.3)
                ser.reset_input_buffer()
                send(ser, 'G21')
                send(ser, 'G90')
                pen_up(ser)
                for st in sub:
                    move_to(ser, *st[0])
                    pen_down(ser)
                    for x, y in st[1:]:
                        line_to(ser, x, y)
                    pen_up(ser)
                ser.close()
                break
            except serial.SerialException:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                if attempt == 3:
                    raise
                time.sleep(2)   # USB 復帰を待って再接続
        print(f'  {min(i + chunk, n)}/{n} 本')
        i += chunk


def main():
    global ORIGIN_X, ORIGIN_Y, X_MAX, Y_MAX
    argv = list(sys.argv[1:])
    dry = '--dry' in argv
    frame = '--frame' in argv
    vertical = '--vertical' in argv
    humanize = '--plain' not in argv   # 既定は手書き風ON、--plain でOFF

    if '--seed' in argv:               # 再現したいとき用（同じ揺れを再現）
        i = argv.index('--seed')
        random.seed(int(argv[i + 1]))
        del argv[i:i + 2]

    if '--origin' in argv:
        i = argv.index('--origin')
        ORIGIN_X = float(argv[i + 1])
        ORIGIN_Y = float(argv[i + 2])
        del argv[i:i + 3]

    if '--paper' in argv:
        i = argv.index('--paper')
        X_MAX = float(argv[i + 1])
        Y_MAX = float(argv[i + 2])
        del argv[i:i + 3]

    args = [a for a in argv if a not in ('--dry', '--frame', '--vertical', '--plain')]

    if not args:
        print('使い方: python kanji2gcode.py "文字" [高さmm] [--vertical] [--plain] [--origin X Y] [--paper W H] [--seed N] [--frame] [--dry]')
        print('  --vertical   縦書き（上→下、改行で右→左の列）')
        print('  --plain      手書き風ゆらぎを切る（達筆・きっちり）。既定はゆらぎON')
        print('  --seed N     ゆらぎを固定（同じ揺れを再現）')
        print('  改行は文字列中に \\n を入れる（例 "池本光輝\\n株式会社"）')
        sys.exit(1)

    text = args[0].replace('\\n', '\n')   # コマンドラインの \n を改行に
    height = float(args[1]) if len(args) > 1 else DEFAULT_HEIGHT

    print(f'用紙/可動上限 : X[0〜{X_MAX}]  Y[0〜{Y_MAX}] (mm)')
    print(f'テキスト      : {text!r}  文字高さ {height}mm  原点 ({ORIGIN_X},{ORIGIN_Y})  '
          f'{"縦書き" if vertical else "横書き"}  {"手書き風" if humanize else "達筆(plain)"}')
    strokes, missing = text_to_strokes(text, height, vertical, humanize)
    if missing:
        print(f'⚠️ 筆画データが無い文字（飛ばします）: {" ".join(missing)}')
        print(f'   → fonts/kanjivg/ にその文字の SVG を追加すれば書けます。')
    if not strokes:
        print("描く筆画がありません。")
        sys.exit(1)

    x0, x1, y0, y1 = bounds(strokes)
    print(f'描画範囲      : X[{x0:.1f}〜{x1:.1f}]  Y[{y0:.1f}〜{y1:.1f}]  '
          f'(幅{x1-x0:.1f} × 高{y1-y0:.1f}mm, 筆画数 {len(strokes)})')

    if x1 > X_MAX or y1 > Y_MAX or x0 < 0 or y0 < 0:
        print("⚠️ 紙からはみ出します。文字高さを小さくするか --origin で位置調整を。")
        sys.exit(1)

    if dry:
        print("--dry なので実機は動かしません。範囲チェックのみ。")
        return

    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        if frame:
            print("--frame: 枠をなぞります（書きません）。位置を確認してください。")
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
