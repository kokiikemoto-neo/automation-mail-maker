# -*- coding: utf-8 -*-
"""
輪郭フォントの単線化（線画）→ Gコード → ロボット手書き
------------------------------------------------------------------
任意の輪郭フォント（夜すがら・白妙など手書き/カジュアル系）の文字を
画像化 → 中心線を抽出（skeletonize）→ 線をたどって一本線で書く。
これにより「カジュアルな字形」を「単線（二重線でない）」で書ける。

依存: pip install scikit-image sknw freetype-py numpy

使い方:
  python skeleton2gcode.py "あいうえお" 10 --font fonts\yosugara.ttf
  python skeleton2gcode.py "あ" 15 --font fonts\yosugara.ttf --dry
  python skeleton2gcode.py "あ" 15 --font fonts\yosugara.ttf --frame

ペン制御・送信・描画は kanji2gcode.py を再利用。横書きのみ（縦書きは今後）。
"""

import sys
import time
import random

try:
    import numpy as np
    import freetype
    from skimage.morphology import skeletonize
    import sknw
except ImportError as e:
    print(f"ライブラリ不足: {e}")
    print("pip install scikit-image sknw freetype-py numpy")
    sys.exit(1)

import kanji2gcode as kg

EM_PX     = 256     # 文字をラスタライズする解像度（高いほど精細・重い）
THRESHOLD = 100     # 二値化のしきい値（0-255）
MIN_EDGE_PX = 4     # これより短い骨格の枝（ヒゲ）は捨てる
SAMPLE_PX = 3       # 骨格の点を何pxごとに拾うか（間引き＝なめらか＆軽量）

# 手書きらしい不揃い（mm単位）。機械的な整列を崩す。
JIT_POS_MM   = 0.25   # 文字ごとの位置ずれ
JIT_HEAD_MM  = 2.5    # 改行後の行頭/列頭ずらし
JIT_ADV_MM   = 0.2    # 文字送りの変動
BIG_JIT_PROB = 0.12   # たまに大きくずれる確率（手書き感）
BIG_JIT_MULT = 3.0    # その時の倍率

# 字形の縦横比（1.0=そのまま）。少し縦長にする。
GLYPH_X = 0.90   # 横方向の倍率（細く）
GLYPH_Y = 1.12   # 縦方向の倍率（長く）


def is_kana(ch):
    """ひらがな判定（カタカナは含めない）。"""
    return 0x3040 <= ord(ch) <= 0x309F


def fib_mod3_seq(n):
    """フィボナッチ数列を3で割った余りの列。0,1,1,2,0,2,2,1,0,... (周期8)。
    決定的だが不規則に見えるので、段差の量に使うと自然な手書き感が出る。"""
    seq = []
    a, b = 0, 1
    for _ in range(n):
        seq.append(a % 3)
        a, b = b, a + b
    return seq


def text_to_strokes(text, kanji_h, fontpath, dry_origin=None, vertical=False, kana_h=None):
    """輪郭フォントを単線化し、ロボット座標(mm)のストローク列にする。
    kanji_h=漢字の高さ(mm)、kana_h=ひらがなの高さ(mm、未指定なら漢字と同じ)。
    vertical=True で縦書き（上→下、改行で右→左の列）。座標はすべて mm。"""
    face = freetype.Face(fontpath)
    face.set_pixel_sizes(0, EM_PX)
    if kana_h is None:
        kana_h = kanji_h

    raw = []          # mm座標（y上向き・文字配置済み）
    missing = []
    line_h = kanji_h * 1.5      # 横書きの行送り(mm)
    col_pitch = kanji_h * 1.55  # 縦書きの列送り(mm)
    fib_seq = fib_mod3_seq(512)     # 行頭/列頭の段差パターン
    head_step = kanji_h * 0.5       # 段差1単位＝半角分の大きさ(mm)

    pen = 0.0    # 横書き：文字送り(mm)
    line = 0     # 横書き：行番号
    ycur = 0.0   # 縦書き：列内の縦位置(mm、下へ負)
    col = 0      # 縦書き：列番号

    def jit():
        v = random.uniform(-JIT_POS_MM, JIT_POS_MM)
        if random.random() < BIG_JIT_PROB:    # たまに大きくずらす
            v *= BIG_JIT_MULT
        return v

    for idx, ch in enumerate(text):
        if ch == '\n':
            if vertical:
                col += 1
                ycur = -fib_seq[col % len(fib_seq)] * head_step   # フィボナッチmod3の段差
            else:
                line += 1
                pen = fib_seq[line % len(fib_seq)] * head_step
            continue
        # 「お慶び」などの接頭辞の「お」（次が漢字）は漢字サイズ、他のひらがなは小さく
        nxt = text[idx + 1] if idx + 1 < len(text) else ''
        if ch in ('お', 'ご') and nxt and nxt not in ('\n', '　', ' ') and not is_kana(nxt):
            ch_h = kanji_h   # 「お慶び」「ご清栄」などの接頭辞は漢字サイズ
        elif is_kana(ch):
            ch_h = kana_h
        else:
            ch_h = kanji_h
        scale = ch_h / EM_PX
        if ch.isspace() or ch == '　':
            if vertical:
                ycur -= ch_h * 1.05    # 字下げ・段差に使える
            else:
                pen += ch_h * 0.5
            continue
        if face.get_char_index(ord(ch)) == 0:
            missing.append(ch)
            if vertical:
                ycur -= ch_h * 1.05
            else:
                pen += ch_h * 0.6
            continue
        cdx, cdy = jit(), jit()
        face.load_char(ch, freetype.FT_LOAD_RENDER)
        adv = face.glyph.advance.x / 64.0
        bmp = face.glyph.bitmap
        rows, width = bmp.rows, bmp.width
        bl = face.glyph.bitmap_left
        bt = face.glyph.bitmap_top
        if rows > 0 and width > 0:
            arr = np.array(bmp.buffer, dtype=np.uint8).reshape(rows, width)
            binary = arr > THRESHOLD
            if binary.any():
                skel = skeletonize(binary)
                if skel.any():
                    graph = sknw.build_sknw(skel.astype(np.uint16), multi=True)
                    cx = -col * col_pitch          # 縦書き：列中心X
                    cy = ycur - ch_h / 2.0         # 縦書き：文字中心Y
                    for s, e, k in graph.edges(keys=True):
                        ps = graph[s][e][k]['pts']
                        if len(ps) < MIN_EDGE_PX:   # ヒゲは捨てる
                            continue
                        idxs = list(range(0, len(ps), SAMPLE_PX))
                        if idxs[-1] != len(ps) - 1:
                            idxs.append(len(ps) - 1)
                        pts = []
                        for i in idxs:
                            r, c = ps[i]
                            if vertical:
                                x = cx + (c - width / 2.0) * scale * GLYPH_X + cdx
                                y = cy + (rows / 2.0 - r) * scale * GLYPH_Y + cdy
                            else:
                                x = pen + (bl + c) * scale * GLYPH_X + cdx
                                y = -line * line_h + (bt - r) * scale * GLYPH_Y + cdy
                            pts.append((x, y))
                        raw.append(pts)
        if vertical:
            ycur -= ch_h * 1.05 + random.uniform(-JIT_ADV_MM, JIT_ADV_MM)
        else:
            pen += adv * scale + random.uniform(-JIT_ADV_MM, JIT_ADV_MM)

    if not raw:
        return [], missing

    pts_all = [p for st in raw for p in st]
    min_x = min(p[0] for p in pts_all)
    min_y = min(p[1] for p in pts_all)
    ox = kg.ORIGIN_X if dry_origin is None else dry_origin[0]
    oy = kg.ORIGIN_Y if dry_origin is None else dry_origin[1]

    def conv(p):
        return (ox + (p[0] - min_x), oy + (p[1] - min_y))   # 既に mm

    return [[conv(p) for p in st] for st in raw], missing


def main():
    argv = list(sys.argv[1:])
    dry = '--dry' in argv
    frame = '--frame' in argv
    vertical = '--vertical' in argv

    fontpath = None
    if '--font' in argv:
        i = argv.index('--font'); fontpath = argv[i + 1]; del argv[i:i + 2]
    if '--origin' in argv:
        i = argv.index('--origin')
        kg.ORIGIN_X = float(argv[i + 1]); kg.ORIGIN_Y = float(argv[i + 2]); del argv[i:i + 3]
    if '--paper' in argv:
        i = argv.index('--paper')
        kg.X_MAX = float(argv[i + 1]); kg.Y_MAX = float(argv[i + 2]); del argv[i:i + 3]
    kana_h = None
    if '--kana' in argv:
        i = argv.index('--kana')
        kana_h = float(argv[i + 1]); del argv[i:i + 2]

    args = [a for a in argv if a not in ('--dry', '--frame', '--vertical')]
    if not args or not fontpath:
        print('使い方: python skeleton2gcode.py "文字" [漢字高さmm] --font <輪郭フォント> [--kana かな高さmm] [--vertical] [--origin X Y] [--paper W H] [--frame] [--dry]')
        print('  --vertical 縦書き（上→下、改行で右→左の列）')
        print('  --kana N   ひらがなだけ高さN mm にする（漢字と差をつける）')
        sys.exit(1)

    text = args[0].replace('\\n', '\n')
    height = float(args[1]) if len(args) > 1 else 10.0

    print(f'用紙 X[0〜{kg.X_MAX}] Y[0〜{kg.Y_MAX}]  漢字{height}mm/かな{kana_h or height}mm  原点({kg.ORIGIN_X},{kg.ORIGIN_Y})  {"縦書き" if vertical else "横書き"}')
    print(f'フォント(単線化): {fontpath}')
    strokes, missing = text_to_strokes(text, height, fontpath, vertical=vertical, kana_h=kana_h)
    if missing:
        print(f'[注意] このフォントに無い文字（書けません・飛ばします）: {" ".join(missing)}')
    if not strokes:
        print("描く線がありません。")
        sys.exit(1)

    x0, x1, y0, y1 = kg.bounds(strokes)
    print(f'描画範囲: X[{x0:.1f}〜{x1:.1f}] Y[{y0:.1f}〜{y1:.1f}]  '
          f'(幅{x1-x0:.1f}×高{y1-y0:.1f}mm, 線{len(strokes)}本)')
    if x1 > kg.X_MAX or y1 > kg.Y_MAX or x0 < 0 or y0 < 0:
        print("[注意] 紙からはみ出します。サイズか原点を調整してください。")
        sys.exit(1)
    if dry:
        print("--dry：実機は動かしません。")
        return

    if frame:
        ser = kg.open_serial()
        time.sleep(0.3)
        ser.reset_input_buffer()
        try:
            print("--frame: 枠をなぞります（書きません）。")
            kg.draw_frame(ser, strokes)
        finally:
            ser.close()
    else:
        print("書き始めます（分割送信・USB切断に強い）...")
        kg.draw_chunked(strokes)
        print("=== 完了 ===")


if __name__ == '__main__':
    main()
