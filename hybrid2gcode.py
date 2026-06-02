# -*- coding: utf-8 -*-
r"""
ハイブリッド：輪郭フォントの字形 × KanjiVG の正規筆順（実験的）
------------------------------------------------------------------
手書き輪郭フォント（夜すがら・しょかきペン体など）を単線化(skeletonize)し、
その線群を KanjiVG の正規筆順に対応付けて「並べ替え＋向き調整」する。
→ 「フォントの字形」を保ちつつ「正規の書き順」で書くことを狙う。

仕組み:
  1. フォント文字を画像化→中心線抽出（線群）
  2. KanjiVG から正規筆順の筆画を取得
  3. 各線を最も近い筆画に割り当て、筆順どおりに並べ、向きを筆画に合わせる
  ※ かな・漢字は KanjiVG にあるので筆順化できる。英数字は KanjiVG が無いので
     フォントの線そのままの順になる。

依存: scikit-image, sknw, svgpathtools, freetype-py, numpy

使い方:
  python hybrid2gcode.py "池" 15 --font fonts\yosugara.ttf --dry
  python hybrid2gcode.py "池本光輝" 12 --font fonts\yosugara.ttf --origin 20 20 --paper 190 230
"""

import sys
import os
import time

try:
    import numpy as np
    import freetype
    from skimage.morphology import skeletonize
    import sknw
    from svgpathtools import svg2paths
except ImportError as e:
    print(f"ライブラリ不足: {e}")
    sys.exit(1)

import kanji2gcode as kg

EM_PX       = 256
THRESHOLD   = 100
MIN_EDGE_PX = 4
SAMPLE_PX   = 3
KVG_SAMPLES = 16
KANJIVG_DIR = kg.KANJIVG_DIR


def font_skeleton_lines(ch, face):
    """フォント1文字を単線化し、線群（px・baseline基準・y上向き）を返す。"""
    face.load_char(ch, freetype.FT_LOAD_RENDER)
    bmp = face.glyph.bitmap
    rows, width = bmp.rows, bmp.width
    if rows == 0 or width == 0:
        return []
    arr = np.array(bmp.buffer, dtype=np.uint8).reshape(rows, width)
    binary = arr > THRESHOLD
    if not binary.any():
        return []
    skel = skeletonize(binary)
    if not skel.any():
        return []
    graph = sknw.build_sknw(skel.astype(np.uint16), multi=True)
    bl, bt = face.glyph.bitmap_left, face.glyph.bitmap_top
    lines = []
    for s, e, k in graph.edges(keys=True):
        ps = graph[s][e][k]['pts']
        if len(ps) < MIN_EDGE_PX:
            continue
        idxs = list(range(0, len(ps), SAMPLE_PX))
        if idxs[-1] != len(ps) - 1:
            idxs.append(len(ps) - 1)
        pts = [(bl + ps[i][1], bt - ps[i][0]) for i in idxs]   # x=右, y=上向き
        if len(pts) >= 2:
            lines.append(pts)
    return lines


def load_kanjivg(ch):
    """KanjiVG の筆画を正規筆順で返す（109座標・y上向きに揃える）。無ければ None。"""
    cp = format(ord(ch), '05x')
    f = os.path.join(KANJIVG_DIR, cp + '.svg')
    if not os.path.exists(f):
        return None
    paths, _ = svg2paths(f)
    strokes = []
    for path in paths:
        try:
            pts = [(path.point(i / KVG_SAMPLES).real,
                    -path.point(i / KVG_SAMPLES).imag) for i in range(KVG_SAMPLES + 1)]
        except Exception:
            continue
        if len(pts) >= 2:
            strokes.append(pts)
    return strokes or None


def _normalizer(strokes):
    pts = [p for st in strokes for p in st]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = (maxx - minx) or 1.0
    h = (maxy - miny) or 1.0
    return lambda p: ((p[0] - minx) / w, (p[1] - miny) / h)


def reorder_by_kanjivg(skel_lines, kvg_strokes):
    """スケルトン線群を KanjiVG 筆順に並べ替え、各線の向きも筆画に合わせる。"""
    if not kvg_strokes or not skel_lines:
        return skel_lines
    nm_s = _normalizer(skel_lines)
    nm_k = _normalizer(kvg_strokes)

    kvg_mid, kvg_start = [], []
    for ks in kvg_strokes:
        nk = [nm_k(p) for p in ks]
        kvg_mid.append((sum(p[0] for p in nk) / len(nk), sum(p[1] for p in nk) / len(nk)))
        kvg_start.append(nk[0])

    # 各スケルトン線を最も近い筆画へ割り当て
    assign = [[] for _ in kvg_strokes]
    for li, line in enumerate(skel_lines):
        ns = [nm_s(p) for p in line]
        smid = (sum(p[0] for p in ns) / len(ns), sum(p[1] for p in ns) / len(ns))
        j = min(range(len(kvg_mid)),
                key=lambda j: (smid[0] - kvg_mid[j][0]) ** 2 + (smid[1] - kvg_mid[j][1]) ** 2)
        assign[j].append((li, ns[0], ns[-1]))

    ordered = []
    for j, group in enumerate(assign):
        ks = kvg_start[j]
        # 筆画の始点に近い線から描く
        group.sort(key=lambda it: min(
            (it[1][0] - ks[0]) ** 2 + (it[1][1] - ks[1]) ** 2,
            (it[2][0] - ks[0]) ** 2 + (it[2][1] - ks[1]) ** 2))
        for li, a, b in group:
            line = skel_lines[li]
            da = (a[0] - ks[0]) ** 2 + (a[1] - ks[1]) ** 2
            db = (b[0] - ks[0]) ** 2 + (b[1] - ks[1]) ** 2
            if db < da:               # 筆画の向きに合わせて反転
                line = line[::-1]
            ordered.append(line)
    return ordered


def text_to_strokes(text, height, fontpath):
    face = freetype.Face(fontpath)
    face.set_pixel_sizes(0, EM_PX)
    px_to_mm = height / EM_PX

    raw, missing = [], []
    pen = 0.0
    line = 0
    line_h = EM_PX * 1.4

    for ch in text:
        if ch == '\n':
            line += 1; pen = 0.0; continue
        if ch.isspace() or ch == '　':
            pen += EM_PX * 0.5; continue
        if face.get_char_index(ord(ch)) == 0:
            missing.append(ch); pen += EM_PX * 0.6; continue
        lines = font_skeleton_lines(ch, face)
        adv = face.glyph.advance.x / 64.0
        lines = reorder_by_kanjivg(lines, load_kanjivg(ch))
        for ln in lines:
            raw.append([(pen + x, y - line * line_h) for (x, y) in ln])
        pen += adv

    if not raw:
        return [], missing
    pts_all = [p for st in raw for p in st]
    min_x = min(p[0] for p in pts_all)
    min_y = min(p[1] for p in pts_all)

    def conv(p):
        return (kg.ORIGIN_X + (p[0] - min_x) * px_to_mm,
                kg.ORIGIN_Y + (p[1] - min_y) * px_to_mm)

    return [[conv(p) for p in st] for st in raw], missing


def main():
    argv = list(sys.argv[1:])
    dry = '--dry' in argv
    frame = '--frame' in argv
    fontpath = None
    if '--font' in argv:
        i = argv.index('--font'); fontpath = argv[i + 1]; del argv[i:i + 2]
    if '--origin' in argv:
        i = argv.index('--origin')
        kg.ORIGIN_X = float(argv[i + 1]); kg.ORIGIN_Y = float(argv[i + 2]); del argv[i:i + 3]
    if '--paper' in argv:
        i = argv.index('--paper')
        kg.X_MAX = float(argv[i + 1]); kg.Y_MAX = float(argv[i + 2]); del argv[i:i + 3]

    args = [a for a in argv if a not in ('--dry', '--frame')]
    if not args or not fontpath:
        print('使い方: python hybrid2gcode.py "文字" [高さmm] --font <輪郭フォント> [--origin X Y] [--paper W H] [--frame] [--dry]')
        sys.exit(1)

    text = args[0].replace('\\n', '\n')
    height = float(args[1]) if len(args) > 1 else 12.0

    print(f'用紙 X[0〜{kg.X_MAX}] Y[0〜{kg.Y_MAX}]  高さ{height}mm  原点({kg.ORIGIN_X},{kg.ORIGIN_Y})')
    print(f'フォント字形 × KanjiVG筆順: {fontpath}')
    strokes, missing = text_to_strokes(text, height, fontpath)
    if missing:
        print(f'[注意] このフォントに無い文字（書けません）: {" ".join(missing)}')
    if not strokes:
        print("描く線がありません。")
        sys.exit(1)

    x0, x1, y0, y1 = kg.bounds(strokes)
    print(f'描画範囲: X[{x0:.1f}〜{x1:.1f}] Y[{y0:.1f}〜{y1:.1f}]  (線{len(strokes)}本)')
    if x1 > kg.X_MAX or y1 > kg.Y_MAX or x0 < 0 or y0 < 0:
        print("[注意] 紙からはみ出します。")
        sys.exit(1)
    if dry:
        print("--dry：実機は動かしません。")
        return

    ser = kg.serial.Serial(kg.SERIAL_PORT, kg.BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        if frame:
            kg.draw_frame(ser, strokes)
        else:
            print("書き始めます...")
            kg.draw_strokes(ser, strokes)
            print("=== 完了 ===")
    finally:
        ser.close()


if __name__ == '__main__':
    main()
