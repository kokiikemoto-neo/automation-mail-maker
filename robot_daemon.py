# -*- coding: utf-8 -*-
"""
ロボットPC 常駐プログラム（Phase 6）
------------------------------------------------------------------
スプレッドシート（注文キュー）を数秒ごとに見に行き、
ステータス「待ち」の注文を1件ずつ取り出して、ロボットで便箋に手書きする。

  待ち → （このプログラムが拾う）→ 処理中 → 手書き → 完了
                                           └（失敗）→ エラー（備考に理由）

クラウド側（GASウェブアプリ）が積んだ注文を、ここが順番に処理する。
手書きは skeleton2gcode.py（ひとりゴシックを単線化＝カジュアルな手書き感）で行う。
設定は config.py に集約。文字サイズ等の調整は config.py を編集。

────────────────────────────────────────────────
事前準備（詳しくは ROBOT_PC_SETUP.md）:
  1. pip install -r requirements-daemon.txt
  2. サービスアカウントの鍵 service_account.json をこのフォルダに置く
  3. 下の SETTINGS を自分の環境に合わせる（スプレッドシートID・COMポート・紙サイズ）
  4. python robot_daemon.py        # 常駐開始（Ctrl+C で停止）
     python robot_daemon.py --dry  # ロボットを動かさず、流れだけ確認
────────────────────────────────────────────────
"""

import os
import sys
import time
import datetime
import threading

# Windows の日本語コンソール(cp932)でも、絵文字・記号で print が落ちないように UTF-8 出力にする
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# --- 依存ライブラリ ---
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Google 連携ライブラリが未インストールです: pip install -r requirements-daemon.txt")
    sys.exit(1)

try:
    import serial  # noqa: F401  （kanji2gcode 内でも使うが、ここでも存在確認）
except ImportError:
    print("pyserial が未インストールです: pip install pyserial")
    sys.exit(1)

import kanji2gcode as kg          # 通信・ペン制御・設定（config経由）・分割送信
import skeleton2gcode as sk       # 本番の書字エンジン（ひとりゴシックを単線化＝手書き感）


# ======================== SETTINGS（ここを自分の環境に） ========================
SERVICE_ACCOUNT_FILE = 'service_account.json'  # サービスアカウントの鍵ファイル
SPREADSHEET_ID = '1iaVOompgIbpNwyCzrWBwHCSQDzOEvlrFkyjNaTBScDU'  # 注文キューのスプレッドシートID（URLの /d/ と /edit の間）
SPREADSHEET_NAME = '便箋作成キュー（自動生成）'  # ↑が空のとき、名前で開く（要 Drive 共有）
ORDERS_SHEET = 'Orders'        # シート名（GAS側と一致）
CONTROL_SHEET = 'Control'      # 操作シート名（一時停止/再開/中止。GAS側と一致）

POLL_SECONDS = 5               # 何秒ごとにキューを見に行くか
CONTROL_POLL_SECONDS = 2       # 何秒ごとに操作（停止/再開/中止）を確認するか

# 用紙・レイアウト（便箋に合わせて調整）
PAPER_W = 190.0                # 紙の幅(mm)。kg.X_MAX に渡す
PAPER_H = 270.0                # 紙の高さ(mm)。kg.Y_MAX に渡す
ORIGIN_X = 20.0                # 書き出し原点X(mm)
ORIGIN_Y = 20.0               # 書き出し原点Y(mm)
CHAR_HEIGHT = 7.0              # 漢字の高さ(mm)。かなは config.KANA_HEIGHT（5mm）
VERTICAL = True               # True=縦書き（便箋向き）
WRITE_RECIPIENT = True        # 文面の前に宛名（○○様）を入れて書くか
SERIAL_PORT = 'COM3'          # ロボットのCOMポート（デバイスマネージャーで確認）
USE_STREAMING = False         # True で高速ストリーミング送信（まず False で検証）
# ===========================================================================

# 注文キューの列（GAS側 Code.gs の COL と一致させること）
COL_ID = 1
COL_RECEIVED = 2
COL_STAFF = 3
COL_COMPANY = 4
COL_RECIPIENT = 5
COL_MESSAGE = 6
COL_STATUS = 7
COL_PROCESSED = 8
COL_NOTE = 9

STATUS_WAIT = '待ち'
STATUS_DOING = '処理中'
STATUS_DONE = '完了'
STATUS_ERROR = 'エラー'
STATUS_CANCEL = 'キャンセル'

# Control シートのセル（GAS側と一致）
CTL_ACTION = 'B1'   # 指示（画面が書く）: 一時停止 / 再開 / 中止
CTL_STATE = 'B2'    # 状態（このプログラムが書く）
CTL_CURRENT = 'B3'  # 処理中の注文ID（このプログラムが書く）
CTL_UPDATED = 'B4'  # 更新時刻（このプログラムが書く）

DRY_RUN = '--dry' in sys.argv


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


class Cancelled(Exception):
    """中止操作で書き出しを打ち切ったときに投げる。"""
    pass


class Control:
    """画面からの操作（一時停止/再開/中止）を保持する、スレッド共有の状態。"""
    def __init__(self):
        self.paused = False
        self.cancel = threading.Event()
        self.stop = threading.Event()


# ---------------------- スプレッドシート接続 ----------------------
def _connect_spreadsheet():
    """サービスアカウントで接続し、スプレッドシート(sh)を返す。
    メインと監視スレッドは別々にこれを呼び、各自のクライアントを持つ
    （gspread のセッションをスレッド間で共有しないため）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(here, SERVICE_ACCOUNT_FILE)
    if not os.path.exists(key_path):
        log(f'鍵ファイルが見つかりません: {key_path}')
        log('ROBOT_PC_SETUP.md の手順で service_account.json を置いてください。')
        sys.exit(1)

    scopes = ['https://www.googleapis.com/auth/spreadsheets',
              'https://www.googleapis.com/auth/drive.readonly']  # 名前で開くとき用
    creds = Credentials.from_service_account_file(key_path, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else gc.open(SPREADSHEET_NAME)


def connect_sheet():
    return _connect_spreadsheet().worksheet(ORDERS_SHEET)


def get_control_ws(sh):
    """Control シートを取得。無ければ作る（画面を一度も開く前でも動くように）。"""
    try:
        return sh.worksheet(CONTROL_SHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=CONTROL_SHEET, rows=10, cols=2)
        ws.update([['指示', ''], ['状態', '待機中'],
                   ['処理中の注文', ''], ['更新時刻', '']], 'A1:B4')
        return ws


# ---------------------- 状態の書き戻し（画面に見せる）----------------------
_last = {'state': None, 'cur': None, 'ts': 0.0}


def set_state(control_ws, state, current=''):
    """状態が変わったとき＋15秒ごと（生存確認）だけ書き込む。書きすぎ防止。"""
    now = time.time()
    if state == _last['state'] and current == _last['cur'] and (now - _last['ts'] < 15):
        return
    _last.update(state=state, cur=current, ts=now)
    stamp = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    try:
        control_ws.update_acell(CTL_STATE, state)
        control_ws.update_acell(CTL_CURRENT, current)
        control_ws.update_acell(CTL_UPDATED, stamp)
    except Exception as e:
        log(f'状態の書き込み失敗（継続）: {e}')


# ---------------------- 操作の監視（別スレッド・自前のクライアント）----------------------
def control_watcher(ctl):
    try:
        cws = get_control_ws(_connect_spreadsheet())
    except Exception as e:
        log(f'操作シートに接続できません（操作機能オフで継続）: {e}')
        return
    while not ctl.stop.is_set():
        try:
            action = (cws.acell(CTL_ACTION).value or '').strip()
            if action == '一時停止':
                if not ctl.paused:
                    log('操作：一時停止')
                ctl.paused = True
            elif action == '再開':
                if ctl.paused:
                    log('操作：再開')
                ctl.paused = False
                cws.update_acell(CTL_ACTION, '')   # 指示を消費
            elif action == '中止':
                log('操作：中止（いまの1通を打ち切り）')
                ctl.cancel.set()
                ctl.paused = False
                cws.update_acell(CTL_ACTION, '')   # 指示を消費
        except Exception:
            pass  # 一時的なAPIエラーは無視して次の周回で
        ctl.stop.wait(CONTROL_POLL_SECONDS)


# ---------------------- 注文の取り出し ----------------------
def find_next_order(ws):
    """ステータス『待ち』の最初の行を返す。無ければ None。
    返り値: (row_number, order_dict)"""
    values = ws.get_all_values()
    for i, row in enumerate(values[1:], start=2):  # 1行目はヘッダ
        status = row[COL_STATUS - 1] if len(row) >= COL_STATUS else ''
        if status.strip() == STATUS_WAIT:
            order = {
                'row': i,
                'id': row[COL_ID - 1] if len(row) >= COL_ID else '',
                'company': row[COL_COMPANY - 1] if len(row) >= COL_COMPANY else '',
                'recipient': row[COL_RECIPIENT - 1] if len(row) >= COL_RECIPIENT else '',
                'message': row[COL_MESSAGE - 1] if len(row) >= COL_MESSAGE else '',
            }
            return order
    return None


def set_status(ws, row, status, note=None):
    ws.update_cell(row, COL_STATUS, status)
    if status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCEL):
        ws.update_cell(row, COL_PROCESSED,
                       datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S'))
    if note is not None:
        ws.update_cell(row, COL_NOTE, note)


# ---------------------- 文面の組み立て ----------------------
def wrap_columns(text, max_chars):
    """段落（\n区切り）を、1列 max_chars 文字ずつに折り返して \n でつなぐ。
    縦書きでは各行が1列（上→下）になり、\n で左の列へ移る。"""
    out = []
    for para in str(text).split('\n'):
        para = para.strip()
        if not para:
            continue
        for i in range(0, len(para), max_chars):
            out.append(para[i:i + max_chars])
    return '\n'.join(out)


def build_letter_text(order):
    """注文から、便箋に書くテキスト（折り返し済み）を作る。"""
    # 1列に入る文字数を紙の高さから概算（縦書き：高さ方向に並ぶ）
    pitch_mm = CHAR_HEIGHT * kg.CHAR_GAP
    usable = (PAPER_H - ORIGIN_Y) - CHAR_HEIGHT  # 下端の余白ぶん引く
    max_chars = max(1, int(usable / pitch_mm))

    parts = []
    if WRITE_RECIPIENT and order['recipient'].strip():
        parts.append(order['recipient'].strip())  # 例: 採用ご担当者 / 山田太郎 様
    parts.append(order['message'])
    body = '\n'.join(parts)
    return wrap_columns(body, max_chars)


# ---------------------- 手書き実行 ----------------------
def draw_chunked_control(strokes, ctl, chunk=40):
    """kanji2gcode.draw_chunked と同じ分割送信（40本ごとに接続し直し＝USB切断に強い）。
    加えて、筆画の区切りで一時停止/中止を見る。
    中止時はペンを上げ、原点に戻してから Cancelled を投げる（次の便箋の位置を保つ）。"""
    i = 0
    n = len(strokes)
    while i < n:
        sub = strokes[i:i + chunk]
        for attempt in range(6):
            ser = None
            try:
                ser = kg.open_serial()
                time.sleep(0.3)
                ser.reset_input_buffer()
                kg.send(ser, 'G21')
                kg.send(ser, 'G90')
                kg.pen_up(ser)
                for st in sub:
                    while ctl.paused and not ctl.cancel.is_set():
                        time.sleep(0.3)       # 一時停止中は区切りで待機（ペンは上）
                    if ctl.cancel.is_set():
                        kg.pen_up(ser)
                        kg.move_to(ser, kg.ORIGIN_X, kg.ORIGIN_Y)
                        ser.close()
                        raise Cancelled()
                    kg.move_to(ser, *st[0])
                    kg.pen_down(ser)
                    for x, y in st[1:]:
                        kg.line_to(ser, x, y)
                    kg.pen_up(ser)
                ser.close()
                break
            except serial.SerialException:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                if attempt == 5:
                    raise
                time.sleep(2)                  # USB 復帰を待って再接続
        log(f'  {min(i + chunk, n)}/{n} 本')
        i += chunk
    # 最後に原点へ戻す
    try:
        ser = kg.open_serial()
        time.sleep(0.2)
        kg.move_to(ser, kg.ORIGIN_X, kg.ORIGIN_Y)
        ser.close()
    except Exception:
        pass


def write_letter(text, ctl):
    """テキストをロボットで手書きする。範囲外なら例外を投げる。"""
    # kanji2gcode のグローバル設定をこの注文用に上書き
    kg.X_MAX = PAPER_W
    kg.Y_MAX = PAPER_H
    kg.ORIGIN_X = ORIGIN_X
    kg.ORIGIN_Y = ORIGIN_Y
    kg.SERIAL_PORT = SERIAL_PORT

    # ひとりゴシックを単線化（カジュアルな手書き感）。漢字=CHAR_HEIGHT、かな=config.KANA_HEIGHT
    strokes, missing = sk.text_to_strokes(
        text, CHAR_HEIGHT, kg.FONT_PATH, vertical=VERTICAL, kana_h=kg.KANA_HEIGHT)
    if not strokes:
        raise RuntimeError('描く筆画がありません（空の文面？）')
    if missing:
        log(f'[注意] このフォントに無い文字（飛ばして書きます）: {" ".join(missing)}')

    x0, x1, y0, y1 = kg.bounds(strokes)
    if x1 > kg.X_MAX or y1 > kg.Y_MAX or x0 < 0 or y0 < 0:
        raise RuntimeError(
            f'紙からはみ出します 範囲X[{x0:.0f}-{x1:.0f}] Y[{y0:.0f}-{y1:.0f}] '
            f'/ 紙[{kg.X_MAX:.0f}x{kg.Y_MAX:.0f}]。文字高さか原点を調整してください。')

    log(f'描画範囲 X[{x0:.0f}-{x1:.0f}] Y[{y0:.0f}-{y1:.0f}] 筆画数 {len(strokes)}')

    if DRY_RUN:
        log('--dry：ロボットは動かしません（範囲チェックのみOK）。')
        return

    # 分割送信（40本ごとに接続し直し）＝長文でもUSB切断に強い。操作（停止/中止）対応。
    draw_chunked_control(strokes, ctl)


# ---------------------- 1件処理 ----------------------
def process_one(ws, control_ws, ctl, order):
    log(f'注文 {order["id"]} を処理: {order["company"]} / {order["recipient"]}')
    ctl.cancel.clear()   # 待機中に押された古い中止指示を持ち越さない
    set_status(ws, order['row'], STATUS_DOING)
    set_state(control_ws, '書き込み中', order['id'])
    try:
        text = build_letter_text(order)
        write_letter(text, ctl)
        set_status(ws, order['row'], STATUS_DONE, note='')
        log(f'注文 {order["id"]} 完了 ✅')
    except Cancelled:
        set_status(ws, order['row'], STATUS_CANCEL, note='画面から中止')
        log(f'注文 {order["id"]} を中止しました（次へ進みます）')
    except Exception as e:
        msg = str(e)
        log(f'注文 {order["id"]} エラー ❌: {msg}')
        try:
            set_status(ws, order['row'], STATUS_ERROR, note=msg[:300])
        except Exception as e2:
            log(f'  ※エラー記録にも失敗: {e2}')
    finally:
        ctl.cancel.clear()


# ---------------------- メインループ ----------------------
def main():
    log('ロボットPC 常駐プログラムを開始します' + ('（--dry モード）' if DRY_RUN else ''))
    sh = _connect_spreadsheet()
    ws = sh.worksheet(ORDERS_SHEET)
    control_ws = get_control_ws(sh)
    log(f'キューに接続しました（注文: {ORDERS_SHEET} / 操作: {CONTROL_SHEET}）。'
        f'{POLL_SECONDS}秒ごとに確認します。')
    log('停止するには Ctrl+C。')

    # 操作（一時停止/再開/中止）の監視を別スレッドで開始
    ctl = Control()
    threading.Thread(target=control_watcher, args=(ctl,), daemon=True).start()

    idle_logged = False
    paused_logged = False
    try:
        while True:
            try:
                if ctl.paused:
                    set_state(control_ws, '一時停止中')
                    if not paused_logged:
                        log('一時停止中。再開待ち…')
                        paused_logged = True
                    time.sleep(POLL_SECONDS)
                    continue
                paused_logged = False

                order = find_next_order(ws)
                if order:
                    idle_logged = False
                    process_one(ws, control_ws, ctl, order)
                else:
                    set_state(control_ws, '待機中')
                    if not idle_logged:
                        log('待ちの注文はありません。待機中…')
                        idle_logged = True
                    time.sleep(POLL_SECONDS)
            except Exception as e:
                # 通信切れ・一時的なAPIエラーなどは、止めずに少し待って再試行
                log(f'一時エラー（継続します）: {e}')
                time.sleep(POLL_SECONDS * 2)
    except KeyboardInterrupt:
        log('停止しました。')
    finally:
        ctl.stop.set()


if __name__ == '__main__':
    main()
