# -*- coding: utf-8 -*-
"""
ロボットPC 常駐プログラム（Phase 6）
------------------------------------------------------------------
スプレッドシート（注文キュー）を数秒ごとに見に行き、
ステータス「待ち」の注文を1件ずつ取り出して、ロボットで便箋に手書きする。

  待ち → （このプログラムが拾う）→ 処理中 → 手書き → 完了
                                           └（失敗）→ エラー（備考に理由）

クラウド側（GASウェブアプリ）が積んだ注文を、ここが順番に処理する。
手書きの中身は既存の kanji2gcode.py をそのまま再利用する。

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

import kanji2gcode as kg


# ======================== SETTINGS（ここを自分の環境に） ========================
SERVICE_ACCOUNT_FILE = 'service_account.json'  # サービスアカウントの鍵ファイル
SPREADSHEET_ID = ''            # 注文キューのスプレッドシートID（URLの /d/ と /edit の間）
SPREADSHEET_NAME = '便箋作成キュー（自動生成）'  # ↑が空のとき、名前で開く（要 Drive 共有）
ORDERS_SHEET = 'Orders'        # シート名（GAS側と一致）

POLL_SECONDS = 5               # 何秒ごとにキューを見に行くか

# 用紙・レイアウト（便箋に合わせて調整）
PAPER_W = 190.0                # 紙の幅(mm)。kg.X_MAX に渡す
PAPER_H = 270.0                # 紙の高さ(mm)。kg.Y_MAX に渡す
ORIGIN_X = 20.0                # 書き出し原点X(mm)
ORIGIN_Y = 20.0               # 書き出し原点Y(mm)
CHAR_HEIGHT = 6.0              # 文字の高さ(mm)
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

DRY_RUN = '--dry' in sys.argv


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)


# ---------------------- スプレッドシート接続 ----------------------
def connect_sheet():
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

    if SPREADSHEET_ID:
        sh = gc.open_by_key(SPREADSHEET_ID)
    else:
        sh = gc.open(SPREADSHEET_NAME)
    return sh.worksheet(ORDERS_SHEET)


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
    if status in (STATUS_DONE, STATUS_ERROR):
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
def write_letter(text):
    """テキストをロボットで手書きする。範囲外なら例外を投げる。"""
    # kanji2gcode のグローバル設定をこの注文用に上書き
    kg.X_MAX = PAPER_W
    kg.Y_MAX = PAPER_H
    kg.ORIGIN_X = ORIGIN_X
    kg.ORIGIN_Y = ORIGIN_Y
    kg.SERIAL_PORT = SERIAL_PORT

    strokes, missing = kg.text_to_strokes(text, CHAR_HEIGHT, VERTICAL)
    if not strokes:
        raise RuntimeError('描く筆画がありません（空の文面？）')
    if missing:
        log(f'⚠️ 筆画データが無い文字（飛ばして書きます）: {" ".join(missing)}')

    x0, x1, y0, y1 = kg.bounds(strokes)
    if x1 > kg.X_MAX or y1 > kg.Y_MAX or x0 < 0 or y0 < 0:
        raise RuntimeError(
            f'紙からはみ出します 範囲X[{x0:.0f}-{x1:.0f}] Y[{y0:.0f}-{y1:.0f}] '
            f'/ 紙[{kg.X_MAX:.0f}x{kg.Y_MAX:.0f}]。文字高さか原点を調整してください。')

    log(f'描画範囲 X[{x0:.0f}-{x1:.0f}] Y[{y0:.0f}-{y1:.0f}] 筆画数 {len(strokes)}')

    if DRY_RUN:
        log('--dry：ロボットは動かしません（範囲チェックのみOK）。')
        return

    ser = serial.Serial(kg.SERIAL_PORT, kg.BAUD_RATE, timeout=5)
    time.sleep(2)
    ser.reset_input_buffer()
    try:
        if USE_STREAMING:
            kg.draw_strokes_streamed(ser, strokes)
        else:
            kg.draw_strokes(ser, strokes)
    finally:
        ser.close()


# ---------------------- 1件処理 ----------------------
def process_one(ws, order):
    log(f'注文 {order["id"]} を処理: {order["company"]} / {order["recipient"]}')
    set_status(ws, order['row'], STATUS_DOING)
    try:
        text = build_letter_text(order)
        write_letter(text)
        set_status(ws, order['row'], STATUS_DONE, note='')
        log(f'注文 {order["id"]} 完了 ✅')
    except Exception as e:
        msg = str(e)
        log(f'注文 {order["id"]} エラー ❌: {msg}')
        try:
            set_status(ws, order['row'], STATUS_ERROR, note=msg[:300])
        except Exception as e2:
            log(f'  ※エラー記録にも失敗: {e2}')


# ---------------------- メインループ ----------------------
def main():
    log('ロボットPC 常駐プログラムを開始します' + ('（--dry モード）' if DRY_RUN else ''))
    ws = connect_sheet()
    log(f'キューに接続しました（シート: {ORDERS_SHEET}）。{POLL_SECONDS}秒ごとに確認します。')
    log('停止するには Ctrl+C。')

    idle_logged = False
    while True:
        try:
            order = find_next_order(ws)
            if order:
                idle_logged = False
                process_one(ws, order)
            else:
                if not idle_logged:
                    log('待ちの注文はありません。待機中…')
                    idle_logged = True
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log('停止しました。')
            break
        except Exception as e:
            # 通信切れ・一時的なAPIエラーなどは、止めずに少し待って再試行
            log(f'一時エラー（継続します）: {e}')
            time.sleep(POLL_SECONDS * 2)


if __name__ == '__main__':
    main()
