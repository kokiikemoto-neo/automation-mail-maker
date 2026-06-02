# -*- coding: utf-8 -*-
"""
差し込み自動書き出し（Phase 3）
------------------------------------------------------------------
CSV の宛名リストを読み、ロボットで1枚ずつ自動で書く。
各行のセルを改行でつないで縦書きで書く（例: 会社名 / 氏名）。
1枚書き終わるごとに、紙の交換を促して次へ進む。

CSV（1行目はヘッダとして読み飛ばす。UTF-8）:
  会社名,氏名
  株式会社ネオキャリア,池本光輝
  さくら商事株式会社,山田太郎

使い方:
  python batch_write.py addresses.csv             # 既定サイズ・縦書きで全件
  python batch_write.py addresses.csv 5 --vertical
  python batch_write.py addresses.csv 5 --vertical --paper 190 230 --origin 20 20
  python batch_write.py addresses.csv 5 --vertical --dry   # 書かずに各件を確認

ペン制御・座標・フォントはすべて kanji2gcode.py の仕組みを再利用。
"""

import sys
import csv
import time

try:
    import serial
except ImportError:
    print("pyserial が入っていません: pip install pyserial")
    sys.exit(1)

import kanji2gcode as kg

# 描画方式の切替（段階導入）。
#   False = 旧方式 draw_strokes（1点ごとに ok 待ち・実績あり＝既定）
#   True  = 新方式 draw_strokes_streamed（GRBL ストリーミング・高速）
# まずは既定 False のまま --stream で 1〜2 件だけ検証し、問題なければ True に切替。
USE_STREAMING = False


def build_text(row):
    """CSV1行のセルを、空でないものだけ改行でつなぐ。"""
    return '\n'.join(c.strip() for c in row if c.strip())


def build_address(row):
    """
    CSV1行を宛名に整形する。
    - 最後の列を氏名とみなし「　様」を付ける
    - 列が1つ（会社名だけ）なら「御中」を付ける
    - 間の列（部署など）はそのまま
    例: [株式会社ネオキャリア, 池本光輝] → "株式会社ネオキャリア\n池本光輝　様"
    """
    cells = [c.strip() for c in row if c.strip()]
    if not cells:
        return ''
    if len(cells) == 1:
        return cells[0] + '\n御中'
    name = cells[-1] + '　様'
    return '\n'.join(cells[:-1] + [name])


def main():
    argv = list(sys.argv[1:])
    dry = '--dry' in argv
    vertical = '--vertical' in argv
    no_header = '--no-header' in argv
    use_streaming = USE_STREAMING or ('--stream' in argv)   # 既定は定数、--stream で一時ON

    if '--paper' in argv:
        i = argv.index('--paper')
        kg.X_MAX = float(argv[i + 1]); kg.Y_MAX = float(argv[i + 2])
        del argv[i:i + 3]
    if '--origin' in argv:
        i = argv.index('--origin')
        kg.ORIGIN_X = float(argv[i + 1]); kg.ORIGIN_Y = float(argv[i + 2])
        del argv[i:i + 3]

    limit = None
    if '--limit' in argv:
        i = argv.index('--limit')
        limit = int(argv[i + 1])
        del argv[i:i + 2]

    args = [a for a in argv if a not in ('--dry', '--vertical', '--no-header', '--stream')]
    if not args:
        print('使い方: python batch_write.py <CSV> [高さmm] [--vertical] [--origin X Y] [--paper W H] [--limit N] [--dry] [--no-header] [--stream]')
        print('  --stream  新方式（GRBL ストリーミング・高速）で書く。既定は旧方式。')
        sys.exit(1)

    csvfile = args[0]
    height = float(args[1]) if len(args) > 1 else kg.DEFAULT_HEIGHT

    with open(csvfile, encoding='utf-8') as f:
        rows = list(csv.reader(f))
    if not rows:
        print("CSV が空です。")
        sys.exit(1)
    data = rows if no_header else rows[1:]
    data = [r for r in data if any(c.strip() for c in r)]   # 空行を除く
    if not data:
        print("書く宛名がありません（ヘッダだけ？ --no-header も検討）。")
        sys.exit(1)

    print(f'用紙 X[0〜{kg.X_MAX}] Y[0〜{kg.Y_MAX}]  文字高さ {height}mm  '
          f'{"縦書き" if vertical else "横書き"}  原点({kg.ORIGIN_X},{kg.ORIGIN_Y})  '
          f'送信方式: {"新(ストリーミング)" if use_streaming else "旧(逐次)"}')
    print(f'{len(data)} 件を書きます。\n')

    # 事前に全件の範囲チェック（はみ出しを書く前に発見）
    plans = []
    for idx, row in enumerate(data, 1):
        text = build_address(row)
        strokes, missing = kg.text_to_strokes(text, height, vertical)
        if not strokes:
            print(f'[{idx}] "{text}" → 描く筆画なし。スキップ。')
            continue
        x0, x1, y0, y1 = kg.bounds(strokes)
        over = x1 > kg.X_MAX or y1 > kg.Y_MAX or x0 < 0 or y0 < 0
        flag = '⚠️はみ出し' if over else 'OK'
        miss = f'  欠字:{" ".join(missing)}' if missing else ''
        print(f'[{idx}] {text!r}  範囲 X[{x0:.0f}-{x1:.0f}] Y[{y0:.0f}-{y1:.0f}]  {flag}{miss}')
        if not over:
            plans.append((idx, text, strokes))

    if limit is not None:
        plans = plans[:limit]
        print(f'\n--limit {limit}：先頭 {len(plans)} 件だけ書きます。')

    if dry:
        print(f'\n--dry：実機は動かしません。書ける件数 {len(plans)}/{len(data)}。')
        return

    if not plans:
        print("\n書ける件がありません（全件はみ出し等）。サイズや原点を見直してください。")
        return

    print(f'\nポート {kg.SERIAL_PORT} に接続します...')
    ser = serial.Serial(kg.SERIAL_PORT, kg.BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        for n, (idx, text, strokes) in enumerate(plans, 1):
            print(f'\n=== {n}/{len(plans)} 件目: {text!r} を書きます ===')
            if use_streaming:
                kg.draw_strokes_streamed(ser, strokes)
            else:
                kg.draw_strokes(ser, strokes)
            print('  完了。')
            if n < len(plans):
                input('  紙を新しいものに交換したら Enter キーを押してください...')
        print('\n=== 全件の書き出しが完了しました ===')
    finally:
        ser.close()


if __name__ == '__main__':
    main()
