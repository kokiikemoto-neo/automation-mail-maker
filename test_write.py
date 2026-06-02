# -*- coding: utf-8 -*-
"""
Writing Robot T-A4 書字品質テスト
------------------------------------------------
段階的に書字品質を確認するためのスクリプト。
いきなり漢字を書かせず、直線 → 図形 → ひらがな の順で試す。

使い方:
  python test_write.py 1   # ステップ1: 直線テスト（横線・縦線）
  python test_write.py 2   # ステップ2: 四角形テスト
  python test_write.py 3   # ステップ3: ひらがな「し」

最初は必ず 1 から。1がきれいに書けたら 2、2が通ったら 3 に進む。

※ ペンの上下は Z軸で制御する（このロボットの実機検証で確定）。
   Z=10 で紙に着く（ペン下げ）、Z=0 で紙から離れる（ペン上げ）。
   ※ Z軸の向きは一般と逆（数字が大きいほど下がる）。サーボ(M3/M5)方式ではない。
"""

import sys
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません。コマンドプロンプトで:")
    print("    pip install pyserial")
    sys.exit(1)

# ─────────────────────────────────────────────
# 設定（必要に応じてここだけ書き換える）
# ─────────────────────────────────────────────
SERIAL_PORT = 'COM3'      # デバイスマネージャーで確認した番号
BAUD_RATE   = 115200

# ペンの上下は Z軸（mm）。Z=0 が紙に着く位置。
PEN_DOWN_Z = 10.0         # ペンを下げる（紙に着ける）Z値。このロボットは Z=10 で紙に着く（向き逆）
PEN_UP_Z   = 0.0          # ペンを上げる（紙から離す）Z値。Z=0 が一番上
PEN_FEED   = 500          # ペン上下の速度 mm/min

FEED       = 1000         # 描画速度 mm/min（遅いほどきれい・速いほど速い）

# 書き始める基準位置（紙の左下あたり。機械の可動域内で安全な値）
ORIGIN_X = 20
ORIGIN_Y = 20


# ─────────────────────────────────────────────
# 通信まわり
# ─────────────────────────────────────────────
def send(ser, cmd, wait=0.0):
    """1行送って、機械からの応答(ok)を待つ"""
    ser.write((cmd + '\n').encode())
    # GRBL は1コマンドごとに ok を返す。それを読むことで詰まりを防ぐ
    resp = ser.readline().decode(errors='ignore').strip()
    print(f">>> {cmd:<22} | {resp}")
    if wait:
        time.sleep(wait)
    return resp


def setup(ser):
    """共通の初期化"""
    print("--- 初期化 ---")
    send(ser, 'G21')           # ミリメートル単位
    send(ser, 'G90')           # 絶対座標
    pen_up(ser)                # まずペンを上げておく


def pen_up(ser):
    """ペンを上げる（Z を上の位置へ）"""
    send(ser, f'G1 Z{PEN_UP_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.3')   # Z移動が完全に終わるまで待つ（同期）。これが無いとXY移動と混ざる


def pen_down(ser):
    """ペンを下げる（Z を紙に着く位置へ）"""
    send(ser, f'G1 Z{PEN_DOWN_Z:.2f} F{PEN_FEED}')
    send(ser, 'G4 P0.3')   # Z移動が完全に終わるまで待つ（同期）


def move_to(ser, x, y):
    """ペンを上げたまま移動"""
    send(ser, f'G0 X{x:.2f} Y{y:.2f}')


def line_to(ser, x, y):
    """ペンを下げたまま描画"""
    send(ser, f'G1 X{x:.2f} Y{y:.2f} F{FEED}')


def draw_path(ser, points):
    """点列を1本のストロークとして描く。points = [(x,y), (x,y), ...]"""
    if not points:
        return
    move_to(ser, *points[0])   # 始点まで移動（ペンは上がっている）
    pen_down(ser)
    for x, y in points[1:]:
        line_to(ser, x, y)
    pen_up(ser)


# ─────────────────────────────────────────────
# ステップ1: 直線テスト
# ─────────────────────────────────────────────
def step1_lines(ser):
    """横線と縦線を1本ずつ。線がかすれないか・まっすぐか・速度は適切かを見る"""
    ox, oy = ORIGIN_X, ORIGIN_Y
    # 横線 50mm
    draw_path(ser, [(ox, oy), (ox + 50, oy)])
    # 縦線 50mm（横線の右端から上へ）
    draw_path(ser, [(ox + 60, oy), (ox + 60, oy + 50)])
    move_to(ser, ox, oy)


# ─────────────────────────────────────────────
# ステップ2: 四角形テスト
# ─────────────────────────────────────────────
def step2_square(ser):
    """30mm角の四角形。角がきちんと閉じるか・直角が出るかを見る"""
    ox, oy = ORIGIN_X, ORIGIN_Y
    s = 30
    draw_path(ser, [
        (ox, oy),
        (ox + s, oy),
        (ox + s, oy + s),
        (ox, oy + s),
        (ox, oy),          # 始点に戻して閉じる
    ])
    move_to(ser, ox, oy)


# ─────────────────────────────────────────────
# ステップ3: ひらがな「し」（曲線テスト）
# ─────────────────────────────────────────────
def step3_hiragana_shi(ser):
    """
    ひらがな「し」を線分の集まりで近似。
    曲線がなめらかに出るか・小さい文字でも潰れないかを見る。
    （フォントを使う前の、いちばん簡単な曲線確認）
    """
    ox, oy = ORIGIN_X, ORIGIN_Y
    pts = []
    # ① 縦線（上から下へ）— ここは直線でよい
    pts.append((ox + 10, oy + 40))
    pts.append((ox + 10, oy + 18))
    # ② 底のカーブ（3次ベジェ曲線を多点に分割してなめらかに）
    #    点を細かく刻むほど折れ線が目立たず、なめらかな曲線に見える
    p0 = (ox + 10, oy + 18)   # カーブ開始（縦線の下端）
    p1 = (ox + 10, oy + 3)    # 制御点1（いったん真下へ）
    p2 = (ox + 22, oy + 3)    # 制御点2（底を右へ）
    p3 = (ox + 33, oy + 13)   # 終点（右上へ跳ねる）
    SEGMENTS = 24             # 分割数。増やすほどなめらか
    for i in range(1, SEGMENTS + 1):
        t = i / SEGMENTS
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    draw_path(ser, pts)
    move_to(ser, ox, oy)


STEPS = {
    '1': ('直線テスト',       step1_lines),
    '2': ('四角形テスト',     step2_square),
    '3': ('ひらがな「し」',   step3_hiragana_shi),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in STEPS:
        print("使い方: python test_write.py [1|2|3]")
        print("  1 = 直線テスト")
        print("  2 = 四角形テスト")
        print("  3 = ひらがな「し」")
        sys.exit(1)

    step = sys.argv[1]
    label, func = STEPS[step]

    print(f"=== ステップ{step}: {label} ===")
    print(f"ポート {SERIAL_PORT} / {BAUD_RATE}bps に接続します...")

    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
    time.sleep(2)               # GRBL の起動待ち
    ser.reset_input_buffer()
    print("接続完了\n")

    try:
        setup(ser)
        func(ser)
        pen_up(ser)
        print(f"\n=== {label} 完了 ===")
        print("紙を確認してください。線のかすれ・歪み・速度をチェック。")
    finally:
        ser.close()
        print("接続を閉じました。")


if __name__ == '__main__':
    main()
