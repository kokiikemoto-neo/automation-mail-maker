# -*- coding: utf-8 -*-
"""
ペン上げ高さのチューニング
------------------------------------------------------------------
複数の「上げ幅」で、ペンを上げたまま横移動して擦るかを確認する。
各行：左に基準線（必ず書く）→ ペンを上げて右へ移動（ここで擦れば線が出る）。
移動部分に線が出なくなる一番小さい上げ幅が、擦らない最小＝最適値。

使い方:
  python pen_height_test.py
  python pen_height_test.py --origin 20 20
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

import kanji2gcode as kg

HEIGHTS  = [3.0, 2.5, 2.0, 1.5, 1.0]   # 試す上げ幅(mm)。上の行から
ROW_GAP  = 12.0    # 行間(mm)
BASE_LEN = 8.0     # 基準線の長さ(mm)
MOVE_LEN = 45.0    # ペンを上げて移動する距離(mm)＝擦りテスト区間


def main():
    ox, oy = kg.ORIGIN_X, kg.ORIGIN_Y
    if '--origin' in sys.argv:
        i = sys.argv.index('--origin')
        ox = float(sys.argv[i + 1]); oy = float(sys.argv[i + 2])

    print('ペン上げ高さテスト：各行 上げ幅 ' + ' / '.join(f'{h}mm' for h in HEIGHTS))
    ser = kg.open_serial()
    time.sleep(0.5)
    ser.reset_input_buffer()
    kg.send(ser, 'G21')
    kg.send(ser, 'G90')
    try:
        for idx, h in enumerate(HEIGHTS):
            z_up = kg.PEN_DOWN_Z - h    # PEN_DOWN_Z(=10)からh mm上げた位置
            y = oy + idx * ROW_GAP
            # ペンを上げて基準線の始点へ
            kg.send(ser, f'G1 Z{z_up:.2f} F{kg.PEN_FEED}'); kg.send(ser, 'G4 P0.1')
            kg.send(ser, f'G0 X{ox:.2f} Y{y:.2f}')
            # ペンを下げて基準線（ここは必ず紙に出る）
            kg.send(ser, f'G1 Z{kg.PEN_DOWN_Z:.2f} F{kg.PEN_FEED}'); kg.send(ser, 'G4 P0.1')
            kg.send(ser, f'G1 X{ox + BASE_LEN:.2f} Y{y:.2f} F{kg.FEED}')
            # ペンを上げ幅h だけ上げて、右へ移動（ここで擦れば線が出る）
            kg.send(ser, f'G1 Z{z_up:.2f} F{kg.PEN_FEED}'); kg.send(ser, 'G4 P0.1')
            kg.send(ser, f'G0 X{ox + BASE_LEN + MOVE_LEN:.2f} Y{y:.2f}')
            print(f'  行{idx + 1}（下から）: 上げ幅 {h}mm  Z={z_up:.1f}  Y={y:.1f}')
        # 終了：しっかり上げて原点へ
        kg.send(ser, f'G1 Z{kg.PEN_DOWN_Z - 3:.2f} F{kg.PEN_FEED}')
        kg.send(ser, f'G0 X{ox:.2f} Y{oy:.2f}')
    finally:
        ser.close()

    print('\n=== 完了 ===')
    print('各行の右側（移動部分）に線が出ていたら、その上げ幅では擦っています。')
    print('線が出ない一番小さい上げ幅が最適です。')


if __name__ == '__main__':
    main()
