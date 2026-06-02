# -*- coding: utf-8 -*-
"""
描画速度の before/after 計測ツール（本番非破壊）
------------------------------------------------------------------------------
本番（kanji2gcode.py / batch_write.py）は一切変更しない。
このスクリプトは kanji2gcode の text_to_strokes / 定数 / draw_strokes を
「読み取り専用」で再利用し、次の2方式を同一の点列で比較する:

  旧方式 (old)    : kanji2gcode.draw_strokes（1点ごとに ok を待つ逐次往復）
  新方式 (stream) : GRBL character-counting ストリーミング
                    （未ok のバイト数を数え、128バイト枠の許す限り連続送信）

新方式の安全条件（速度のために省略しない）:
  - error: 応答を検出したら即中断（取りこぼして送り続けない）
  - 終端で未回収の ok を全てフラッシュ（応答途絶はガードで検出）

使い方:
  # ハード不要。点数・コマンド数・推定往復回数だけ出す
  python speed_test.py "輝" 10 --dry
  python speed_test.py "池本光輝" 5 --dry

  # 実機で旧/新を連続実行して所要時間を比較（インクあり・紙交換あり）
  python speed_test.py "光" 10 --method both
  python speed_test.py "輝" 10 --method both --nopen   # ペンを下ろさず運動のみ計測

オプション:
  --method old|stream|both   既定 both
  --dry                      実機に接続せず集計のみ
  --nopen                    ペンを下ろさない（インク無しで運動時間だけ測る）
  --vertical / --plain / --seed N / --origin X Y   kanji2gcode と同じ意味
"""

import sys
import time

import kanji2gcode as kg   # 読み取り専用で再利用（本番ファイルは変更しない）

try:
    import serial
except ImportError:
    serial = None   # --dry のときはシリアル不要

# GRBL の受信バッファ（character-counting の上限）
RX_BUFFER_SIZE = 128


# ─────────────────────────────────────────────
# G コード生成（draw_strokes と同一の命令列を“文字列リスト”として作る）
#   ※ 旧/新で全く同じ命令列を流し、差を「送信方式だけ」に限定する
# ─────────────────────────────────────────────
def build_gcode(strokes, nopen=False, dwell_up=0.15, dwell_down=0.05):
    """draw_strokes と同じ命令列を (line, pen_sync) のタプル列で作る。
    pen_sync=True はペン上下後の G4（=ペンが物理的に動ききる待ち）。
    --pen-sync モードでは、この行の ok（dwell 完了後に返る）まで待ってから次へ進む。
    dwell_up / dwell_down で待ち時間を可変（渡り線は up が主因なので個別指定可）。"""
    down_z = kg.PEN_UP_Z if nopen else kg.PEN_DOWN_Z   # nopen はペンを下ろさない
    P = lambda s: ('G4 P%.3f' % s, True)               # ペン待ち行（sync 対象）
    prog = [('G21', False), ('G90', False),
            (f'G1 Z{kg.PEN_UP_Z:.2f} F{kg.PEN_FEED}', False),  # 初期ペン上げ
            P(dwell_up)]
    for st in strokes:
        x0, y0 = st[0]
        prog.append((f'G0 X{x0:.2f} Y{y0:.2f}', False))                 # move_to
        prog.append((f'G1 Z{down_z:.2f} F{kg.PEN_FEED}', False))        # pen_down
        prog.append(P(dwell_down))                                      # 下げ切り待ち
        for x, y in st[1:]:
            prog.append((f'G1 X{x:.2f} Y{y:.2f} F{kg.FEED}', False))    # line_to
        prog.append((f'G1 Z{kg.PEN_UP_Z:.2f} F{kg.PEN_FEED}', False))  # pen_up
        prog.append(P(dwell_up))                                        # 上げ切り待ち（渡り線対策の主役）
    prog.append((f'G0 X{kg.ORIGIN_X:.2f} Y{kg.ORIGIN_Y:.2f}', False))   # 原点へ戻る
    return prog


# ─────────────────────────────────────────────
# 新方式: GRBL character-counting ストリーミング
# ─────────────────────────────────────────────
class StreamError(Exception):
    def __init__(self, msg, errors):
        super().__init__(msg)
        self.errors = errors


def _read_resp(ser):
    return ser.readline().decode(errors='ignore').strip()


def _drain(ser, c_line, errors, where):
    """未回収の ok/error をすべて回収して c_line を空にする。
    error: は即中断。応答途絶（timeout連発）もガードで中断。"""
    empty = 0
    while c_line:
        resp = _read_resp(ser)
        if resp == '':
            empty += 1
            if empty > 3:
                raise StreamError(f'応答が途絶（{where}）', errors)
            continue
        empty = 0
        if resp == 'ok':
            c_line.pop(0)
        elif resp.startswith('error'):
            c_line.pop(0)
            errors.append((where, resp))
            raise StreamError(f'GRBL {resp}（{where}）', errors)
        # 情報行 '[...]' '<...>' は無視


def stream_program(ser, prog, pen_sync=False):
    """未ok のバイト数を数えながら連続送信する。
    prog は (line, pen_sync_flag) のタプル列。
    error: を見たら即中断。終端で全 ok を回収（フラッシュ）。
    pen_sync=True のとき、ペン上下後の G4 では ok（dwell完了後に返る）まで
    バッファを空にし、ペンが物理的に動ききってから次の move を送る。
    戻り値: (elapsed_sec, errors)"""
    c_line = []        # 送信済みで未 ok の各行のバイト長
    errors = []
    ser.reset_input_buffer()
    empty = 0
    t0 = time.perf_counter()

    for i, (raw, is_pen) in enumerate(prog):
        l = raw.strip()
        c_line.append(len(l) + 1)   # +1 は改行ぶん
        # 受信バッファに空きが要る、または応答が来ている間は回収する
        while sum(c_line) >= RX_BUFFER_SIZE - 1 or ser.in_waiting:
            resp = _read_resp(ser)
            if resp == '':
                empty += 1
                if empty > 3:   # 応答途絶（timeout 連発）を検出して中断
                    raise StreamError(f'応答が途絶（送信中・行{i}）', errors)
                continue
            empty = 0
            if resp == 'ok':
                c_line.pop(0)
            elif resp.startswith('error'):
                c_line.pop(0)
                errors.append((i, resp))
                # エラーを握りつぶさず即中断
                raise StreamError(f'GRBL {resp} 付近 行{i}: {l!r}', errors)
            # それ以外（'[...]' '<...>' 等の情報行）は無視
        ser.write((l + '\n').encode())

        # (b) ペン同期モード：ペン上下の G4 はここで完全停止を保証してから進む
        if pen_sync and is_pen:
            _drain(ser, c_line, errors, f'pen-sync 行{i}')

    # 終端フラッシュ: 未回収の ok を全部待つ
    _drain(ser, c_line, errors, 'flush')
    return time.perf_counter() - t0, errors


# ─────────────────────────────────────────────
# 集計（--dry 用）
# ─────────────────────────────────────────────
def summarize(strokes, prog):
    n_strokes = len(strokes)
    n_points = sum(len(st) for st in strokes)
    n_cmds = len(prog)
    n_g1xy = sum(1 for l, _ in prog if l.startswith('G1 X'))   # 描画線分の数
    n_bytes = sum(len(l) + 1 for l, _ in prog)
    return n_strokes, n_points, n_cmds, n_g1xy, n_bytes


def print_dry(strokes, prog, dwell_up, dwell_down):
    n_strokes, n_points, n_cmds, n_g1xy, n_bytes = summarize(strokes, prog)
    print('── 集計（--dry：実機なし）─────────────────')
    print(f'  筆画数            : {n_strokes}')
    print(f'  総点数            : {n_points}')
    print(f'  描画線分(G1 X..)  : {n_g1xy}')
    print(f'  総コマンド数      : {n_cmds}')
    print(f'  総送信バイト      : {n_bytes}')
    print('  ── 旧方式（1コマンド=1往復）──')
    print(f'    推定往復回数    : {n_cmds} 回（毎コマンド ok 待ち）')
    for lat in (0.005, 0.016):   # 1往復 5ms / 16ms(USBレイテンシ既定) の2想定
        print(f'      往復待ちのみ : {n_cmds * lat:6.2f} 秒  (@1往復 {int(lat*1000)}ms)')
    print('  ── 新方式（character-counting）──')
    full = max(1, n_bytes // (RX_BUFFER_SIZE - 1))
    print(f'    ok 待ちで止まる回数 ≒ {full} 回（バッファ満杯時のみ／ほぼ連続送信）')
    dwell_total = dwell_up * (n_strokes + 1) + dwell_down * n_strokes
    print(f'    ペン待ち合計    : {dwell_total:6.2f} 秒  '
          f'(up {dwell_up*1000:.0f}ms×{n_strokes+1} + down {dwell_down*1000:.0f}ms×{n_strokes})')
    print('  ※ 実運動時間は別。上は「往復待ち」「ペン待ち」成分の目安。')


# ─────────────────────────────────────────────
# 実機実行
# ─────────────────────────────────────────────
def open_serial():
    ser = serial.Serial(kg.SERIAL_PORT, kg.BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    return ser


def run_old(ser, strokes, nopen):
    """本番の draw_strokes をそのまま計測。nopen 時のみ PEN_DOWN_Z を一時退避。"""
    saved = kg.PEN_DOWN_Z
    if nopen:
        kg.PEN_DOWN_Z = kg.PEN_UP_Z   # ファイルは変えず、実行時の定数だけ退避/復元
    t0 = time.perf_counter()
    try:
        kg.draw_strokes(ser, strokes)
    finally:
        kg.PEN_DOWN_Z = saved
    return time.perf_counter() - t0


def run_stream(ser, strokes, nopen, dwell_up, dwell_down, pen_sync):
    prog = build_gcode(strokes, nopen=nopen, dwell_up=dwell_up, dwell_down=dwell_down)
    elapsed, errors = stream_program(ser, prog, pen_sync=pen_sync)
    if errors:
        print(f'  ⚠️ GRBL エラー {len(errors)} 件: {errors}')
    return elapsed


# ─────────────────────────────────────────────
# 引数
# ─────────────────────────────────────────────
def parse_args(argv):
    opt = {'method': 'both', 'dry': False, 'nopen': False,
           'vertical': False, 'humanize': True, 'pen_sync': False,
           'dwell_up': 0.15, 'dwell_down': 0.05}
    if '--dry' in argv:    opt['dry'] = True;      argv.remove('--dry')
    if '--nopen' in argv:  opt['nopen'] = True;    argv.remove('--nopen')
    if '--vertical' in argv: opt['vertical'] = True; argv.remove('--vertical')
    if '--plain' in argv:  opt['humanize'] = False; argv.remove('--plain')
    if '--pen-sync' in argv: opt['pen_sync'] = True; argv.remove('--pen-sync')
    if '--seed' in argv:
        import random
        i = argv.index('--seed'); random.seed(int(argv[i + 1])); del argv[i:i + 2]
    if '--origin' in argv:
        i = argv.index('--origin')
        kg.ORIGIN_X = float(argv[i + 1]); kg.ORIGIN_Y = float(argv[i + 2])
        del argv[i:i + 3]
    if '--method' in argv:
        i = argv.index('--method'); opt['method'] = argv[i + 1]; del argv[i:i + 2]
    if '--dwell' in argv:        # 上下まとめて
        i = argv.index('--dwell'); v = float(argv[i + 1])
        opt['dwell_up'] = opt['dwell_down'] = v; del argv[i:i + 2]
    if '--dwell-up' in argv:
        i = argv.index('--dwell-up'); opt['dwell_up'] = float(argv[i + 1]); del argv[i:i + 2]
    if '--dwell-down' in argv:
        i = argv.index('--dwell-down'); opt['dwell_down'] = float(argv[i + 1]); del argv[i:i + 2]
    return opt, argv


def main():
    argv = list(sys.argv[1:])
    opt, argv = parse_args(argv)
    if not argv:
        print('使い方: python speed_test.py "文字" [高さmm] '
              '[--dry] [--method old|stream|both] [--nopen] '
              '[--dwell SEC] [--dwell-up SEC] [--dwell-down SEC] [--pen-sync] '
              '[--vertical] [--plain] [--seed N] [--origin X Y]')
        print('  --dwell-up   ペン上げ後の待ち秒（渡り線対策の主役・既定0.15）')
        print('  --dwell-down ペン下げ後の待ち秒（既定0.05）')
        print('  --pen-sync   ペン上下のG4で必ずバッファを空にしてから次へ（保険）')
        sys.exit(1)

    text = argv[0].replace('\\n', '\n')
    height = float(argv[1]) if len(argv) > 1 else kg.DEFAULT_HEIGHT

    strokes, missing = kg.text_to_strokes(text, height, opt['vertical'], opt['humanize'])
    if missing:
        print(f'⚠️ 筆画データが無い文字（飛ばします）: {" ".join(missing)}')
    if not strokes:
        print('描く筆画がありません。'); sys.exit(1)

    prog = build_gcode(strokes, nopen=opt['nopen'],
                       dwell_up=opt['dwell_up'], dwell_down=opt['dwell_down'])
    x0, x1, y0, y1 = kg.bounds(strokes)
    print(f'テキスト {text!r}  高さ {height}mm  '
          f'範囲 X[{x0:.1f}〜{x1:.1f}] Y[{y0:.1f}〜{y1:.1f}]  筆画 {len(strokes)}')
    print(f'dwell up={opt["dwell_up"]*1000:.0f}ms down={opt["dwell_down"]*1000:.0f}ms  '
          f'pen-sync={"ON" if opt["pen_sync"] else "OFF"}')
    print_dry(strokes, prog, opt['dwell_up'], opt['dwell_down'])

    if opt['dry']:
        print('\n--dry のため実機は動かしません。')
        return

    if serial is None:
        print('pyserial が無いので実機計測はできません（--dry のみ可）。'); sys.exit(1)

    method = opt['method']
    print(f"\n実機計測を開始します（method={method}, "
          f"{'ペン上げのみ' if opt['nopen'] else 'インクあり'}, ポート {kg.SERIAL_PORT}）")

    results = {}
    if method in ('old', 'both'):
        input('【旧方式】を計測します。紙をセットして Enter...')
        ser = open_serial()
        try:
            results['old'] = run_old(ser, strokes, opt['nopen'])
        finally:
            ser.close()
        print(f'  旧方式 所要 {results["old"]:.2f} 秒')

    if method in ('stream', 'both'):
        if method == 'both' and not opt['nopen']:
            input('【新方式】を計測します。新しい紙に交換して Enter...')
        else:
            input('【新方式】を計測します。準備できたら Enter...')
        ser = open_serial()
        try:
            results['stream'] = run_stream(ser, strokes, opt['nopen'],
                                           opt['dwell_up'], opt['dwell_down'],
                                           opt['pen_sync'])
        finally:
            ser.close()
        print(f'  新方式 所要 {results["stream"]:.2f} 秒')

    if 'old' in results and 'stream' in results and results['stream'] > 0:
        ratio = results['old'] / results['stream']
        print('\n── 比較 ─────────────────')
        print(f'  旧方式 : {results["old"]:.2f} 秒')
        print(f'  新方式 : {results["stream"]:.2f} 秒')
        print(f'  速度比 : {ratio:.2f} 倍速  '
              f'(短縮 {results["old"] - results["stream"]:.2f} 秒)')


if __name__ == '__main__':
    main()
