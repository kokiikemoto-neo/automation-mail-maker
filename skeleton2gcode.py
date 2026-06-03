# -*- coding: utf-8 -*-
r"""
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
from config import *   # 設定は config.py に集約（EM_PX, GLYPH_*, JIT_* など）


def optimize_strokes(strokes, gap=1.2):
    """線画はペン上下が多くて遅い。近接する線を連結し、最近傍順に並べ替えて
    ペン上下回数と空走距離を減らす（＝高速化）。完成形の見た目は変わらない。
    gap(mm) 以内の端点同士はペンを上げずに繋ぐ。"""
    if len(strokes) <= 1:
        return strokes
    g2 = gap * gap
    remaining = [list(s) for s in strokes]
    cur = remaining.pop(0)
    out = []
    while remaining:
        cx, cy = cur[-1]
        best_i, best_d2, best_rev = 0, None, False
        for i, st in enumerate(remaining):
            sx, sy = st[0]
            ex, ey = st[-1]
            ds = (sx - cx) ** 2 + (sy - cy) ** 2
            de = (ex - cx) ** 2 + (ey - cy) ** 2
            if best_d2 is None or ds < best_d2:
                best_i, best_d2, best_rev = i, ds, False
            if de < best_d2:
                best_i, best_d2, best_rev = i, de, True
        nxt = remaining.pop(best_i)
        if best_rev:
            nxt = nxt[::-1]
        if best_d2 <= g2:
            cur = cur + nxt          # 近い→ペンを上げず連結
        else:
            out.append(cur)          # 遠い→いったんペンを上げて移動
            cur = nxt
    out.append(cur)
    return out


def wrap_columns(text, max_chars):
    """段落（改行区切り）を1列 max_chars 文字で折り返して改行でつなぐ。
    縦書きでは各行が1列（上→下）になり、紙の高さに収める用。"""
    out = []
    for para in text.split('\n'):
        para = para.rstrip()
        if not para:
            continue
        for i in range(0, len(para), max_chars):
            out.append(para[i:i + max_chars])
    return '\n'.join(out)


def is_kana(ch):
    """ひらがな判定（カタカナは含めない）。"""
    return 0x3040 <= ord(ch) <= 0x309F


# 縦書きのとき90度回して「縦に引く」文字（長音・ダッシュ・波ダッシュ・鉤括弧類）
ROTATE_VERTICAL = ('ー', 'ｰ', '－', '—', '―', '‐', '〜', '～',
                   '「', '」', '『', '』', '（', '）', '(', ')')

# 縦書きで字の右上に小さく置く約物（読点・句点）
PUNCT_TOPRIGHT = ('、', '。', '，', '．')


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

    char_groups = []  # 文字ごとの線リスト（文字単位で最適化＝文字をまたがない）
    missing = []
    line_h = kanji_h * 1.5      # 横書きの行送り(mm)
    col_pitch = kanji_h * 1.55  # 縦書きの列送り(mm)
    fib_seq = fib_mod3_seq(512)         # 行頭/列頭の段差パターン
    head_step = kanji_h * HEAD_STEP_RATIO   # 段差1単位の大きさ(mm)

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
        if ch in PUNCT_TOPRIGHT:
            ch_h = kana_h    # 読点・句点は小さめ
        elif ch in ('お', 'ご') and nxt and nxt not in ('\n', '　', ' ') and not is_kana(nxt):
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
        rot_v = vertical and (ch in ROTATE_VERTICAL)   # 長音・括弧などは縦書きで回す
        punct_v = vertical and (ch in PUNCT_TOPRIGHT)  # 句読点は縦書きで右上に寄せる
        cdx, cdy = jit(), jit()
        face.load_char(ch, freetype.FT_LOAD_RENDER)
        adv = face.glyph.advance.x / 64.0
        bmp = face.glyph.bitmap
        rows, width = bmp.rows, bmp.width
        bl = face.glyph.bitmap_left
        bt = face.glyph.bitmap_top
        cur_lines = []   # この1文字ぶんの線（文字ごとにまとめる）
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
                            if rot_v:
                                # 縦書きの長音・括弧：90度回転（時計回り）
                                x = cx + (rows / 2.0 - r) * scale * GLYPH_X + cdx
                                y = cy - (c - width / 2.0) * scale * GLYPH_Y + cdy
                            elif punct_v:
                                # 句読点：字の右上に寄せる（縦書きの作法）
                                x = cx + (c - width / 2.0) * scale * GLYPH_X + col_pitch * 0.28 + cdx
                                y = cy + (rows / 2.0 - r) * scale * GLYPH_Y + ch_h * 0.30 + cdy
                            elif vertical:
                                x = cx + (c - width / 2.0) * scale * GLYPH_X + cdx
                                y = cy + (rows / 2.0 - r) * scale * GLYPH_Y + cdy
                            else:
                                x = pen + (bl + c) * scale * GLYPH_X + cdx
                                y = -line * line_h + (bt - r) * scale * GLYPH_Y + cdy
                            pts.append((x, y))
                        cur_lines.append(pts)
        if cur_lines:
            char_groups.append(cur_lines)   # 1文字ぶんをまとめて保持
        if vertical:
            ycur -= ch_h * 1.05 + random.uniform(-JIT_ADV_MM, JIT_ADV_MM)
        else:
            pen += adv * scale + random.uniform(-JIT_ADV_MM, JIT_ADV_MM)

    if not char_groups:
        return [], missing

    pts_all = [p for grp in char_groups for line in grp for p in line]
    min_x = min(p[0] for p in pts_all)
    min_y = min(p[1] for p in pts_all)
    ox = kg.ORIGIN_X if dry_origin is None else dry_origin[0]
    oy = kg.ORIGIN_Y if dry_origin is None else dry_origin[1]

    def conv(p):
        return (ox + (p[0] - min_x), oy + (p[1] - min_y))   # 既に mm

    # 文字ごとに連結最適化（文字をまたがない＝一文字ずつ順番に書く・文字飛ばし防止）
    result = []
    for grp in char_groups:
        conv_grp = [[conv(p) for p in line] for line in grp]
        result.extend(optimize_strokes(conv_grp, gap=1.0))
    return result, missing


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
    wrap_n = None
    if '--wrap' in argv:
        i = argv.index('--wrap')
        wrap_n = int(argv[i + 1]); del argv[i:i + 2]

    args = [a for a in argv if a not in ('--dry', '--frame', '--vertical')]
    if not args or not fontpath:
        print('使い方: python skeleton2gcode.py "文字" [漢字高さmm] --font <輪郭フォント> [--kana かな高さmm] [--vertical] [--origin X Y] [--paper W H] [--frame] [--dry]')
        print('  --vertical 縦書き（上→下、改行で右→左の列）')
        print('  --kana N   ひらがなだけ高さN mm にする（漢字と差をつける）')
        sys.exit(1)

    text = args[0].replace('\\n', '\n')
    height = float(args[1]) if len(args) > 1 else 10.0
    if wrap_n:
        text = wrap_columns(text, wrap_n)   # 長文を1列 wrap_n 文字で折り返す

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
