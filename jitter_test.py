# -*- coding: utf-8 -*-
"""
手書き風ゆらぎの比較テスト
------------------------------------------------------------------
5種類のゆらぎ設定で「あ」を横に並べて1枚に書く。
線の震え(PT)は控えめにし、文字ごとの傾き・大きさ・位置のばらつきで
「線はしっかり・字は手書き感」を狙う。左から 1〜5。

使い方:
  python jitter_test.py            # 実機で5つ書く
  python jitter_test.py --dry      # 範囲確認のみ
  python jitter_test.py あ          # 別の文字で試す
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

import kanji2gcode as kg

# (名前, 傾き度, 大きさ率, 位置ずれ, 線の震え)
PRESETS = [
    ("1 ごく控えめ", 1.0, 0.02, 2.0, 0.0),
    ("2 控えめ",     1.5, 0.03, 3.0, 0.4),
    ("3 標準",       2.0, 0.03, 3.5, 0.7),
    ("4 やや強め",   3.0, 0.05, 5.0, 1.0),
    ("5 強め",       4.0, 0.06, 6.0, 1.4),
]


def main():
    dry = '--dry' in sys.argv
    rest = [a for a in sys.argv[1:] if a != '--dry']
    ch = rest[0] if rest else "あ"

    height = 15.0
    gap = 22.0          # 文字の間隔(mm)
    kg.ORIGIN_Y = 30.0

    all_strokes = []
    for i, (name, rot, sc, pos, pt) in enumerate(PRESETS):
        kg.JIT_ROT_DEG = rot
        kg.JIT_SCALE = sc
        kg.JIT_POS = pos
        kg.JIT_PT = pt
        kg.ORIGIN_X = 20.0 + i * gap
        strokes, missing = kg.text_to_strokes(ch, height, False, True)
        if missing:
            print(f'{name}: 文字データ無し {missing}')
            continue
        all_strokes += strokes
        x0, x1, y0, y1 = kg.bounds(strokes)
        print(f'{name:12s} ROT{rot} SC{sc} POS{pos} PT{pt}  X[{x0:.0f}-{x1:.0f}]')

    if not all_strokes:
        print("描く筆画がありません。")
        return
    if dry:
        print(f'\n計 {len(all_strokes)} ストローク。--dry 終了。')
        return

    print(f'\nポート {kg.SERIAL_PORT} に接続して「{ch}」を5種類書きます...')
    ser = serial.Serial(kg.SERIAL_PORT, kg.BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        kg.draw_strokes(ser, all_strokes)
        print("完了。左から 1（ごく控えめ）〜 5（強め）です。")
    finally:
        ser.close()


if __name__ == '__main__':
    main()
