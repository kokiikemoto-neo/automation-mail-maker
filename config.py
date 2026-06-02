# -*- coding: utf-8 -*-
"""
書字エンジン 全設定（本番運用はこのファイルを編集する）
------------------------------------------------------------------
kanji2gcode.py / skeleton2gcode.py / batch_write.py などが
`from config import *` でここを参照する。設定変更は原則このファイルだけ。

このチャットで確定した値を初期値として記載。
"""
import os

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')

# ── 接続 ──
SERIAL_PORT = 'COM3'
BAUD_RATE   = 115200

# ── ペン制御（Z軸・向きは逆＝Z大で下がる）──
PEN_DOWN_Z = 10.0    # 紙に着く位置
PEN_UP_Z   = 7.0     # 移動時の上げ位置（約3mm浮く・擦らない最適値）
PEN_FEED   = 15000   # ペン上下速度（最大付近）
FEED       = 6000    # 描画速度
DWELL      = 0.03    # ペン上下後の同期待ち(秒)

# ── 用紙・書き出し位置 ──
ORIGIN_X = 20.0
ORIGIN_Y = 20.0
X_MAX = 190.0        # 紙サイズ（はみ出しチェック上限）
Y_MAX = 230.0

# ── 文字サイズ(mm) ──
DEFAULT_HEIGHT = 7.0   # 漢字の高さ
KANA_HEIGHT    = 5.0   # ひらがなの高さ（None なら漢字と同じ）

# ── フォント ──
FONT_PATH   = os.path.join(_FONT_DIR, 'HitoriGothic.ttf')   # 本番フォント（単線化して使用）
KANJIVG_DIR = os.path.join(_FONT_DIR, 'kanjivg')            # KanjiVG 筆画（kanji2gcode 用）

# ── 字形比（少し縦長）──
GLYPH_X = 0.90
GLYPH_Y = 1.12

# ── 段差（フィボナッチmod3 × 漢字h × この比）──
HEAD_STEP_RATIO = 0.5

# ── 手書きゆらぎ（mm・skeleton2gcode 用）──
JIT_POS_MM   = 0.25
JIT_HEAD_MM  = 2.5
JIT_ADV_MM   = 0.2
BIG_JIT_PROB = 0.12
BIG_JIT_MULT = 3.0

# ── 線画(skeleton)の内部パラメータ ──
EM_PX       = 256
THRESHOLD   = 100
MIN_EDGE_PX = 4
SAMPLE_PX   = 3
CHUNK       = 40     # 分割送信のチャンク本数

# ── KanjiVG単線(kanji2gcode)の内部パラメータ ──
KVG_EM      = 109.0
ADVANCE     = 109.0
CHAR_GAP    = 1.10
SAMPLE_STEP = 4.0
HUMANIZE    = True
JIT_ROT_DEG = 1.0
JIT_SCALE   = 0.02
JIT_POS     = 2.0
JIT_PT      = 0.0
