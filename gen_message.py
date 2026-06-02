# -*- coding: utf-8 -*-
"""
文面の自動生成（Phase 4）
------------------------------------------------------------------
会社情報と商談状況を入力すると、Claude API が手書きDM向けの
短い文面を生成する。生成→人が承認→手書き（kanji2gcode）という流れ。

事前準備:
  pip install anthropic
  環境変数 ANTHROPIC_API_KEY に API キーを設定

使い方:
  python gen_message.py "株式会社テスト" "効果に繋がるか不安で決裁に戸惑っている"
  → 標準出力に文面。これを確認・修正して kanji2gcode.py で書く。

※ API キーが無くても、文面は人手で用意して kanji2gcode.py に渡せば書ける。
   このスクリプトは「文面づくりを自動化したい」ときに使う。
"""

import os
import sys

try:
    from anthropic import Anthropic
except ImportError:
    print("anthropic SDK が未インストール: pip install anthropic")
    sys.exit(1)

MODEL = "claude-opus-4-8"

# プロンプトは安定（キャッシュ対象）。会社ごとの情報だけを user 側で渡す。
SYSTEM_PROMPT = """あなたは法人営業の「手書きDM」の文面を書くプロのコピーライターです。
便箋に手書きで書き起こす前提で、短く・自然で・相手の状況に寄り添った日本語の文面を作ります。

# 必ず守ること
- 手書きで書ける分量（全体で12行以内、1行は20文字以内を目安）
- テンプレート感を避け、相手の具体的な状況に触れる
- 押し売りせず、相手の不安や迷いに共感し、次の一歩を軽くする提案を入れる
- 「拝啓」で始め「敬具」で結ぶ、丁寧だが固すぎない文体
- 数字や誇張した効果保証はしない
- 出力は文面本文のみ（説明や見出しは付けない）"""


def generate(company, situation, sender=""):
    client = Anthropic()   # ANTHROPIC_API_KEY を使用
    user = (
        f"## 宛先企業\n{company}\n\n"
        f"## これまでの商談状況・相手の心理\n{situation}\n\n"
        f"## 差出人\n{sender or '（差出人名は空欄でよい）'}\n\n"
        "上記を踏まえ、相手の迷いを和らげ、次の面談につながる手書きDMの文面を書いてください。"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def main():
    if len(sys.argv) < 3:
        print('使い方: python gen_message.py "会社名" "商談状況" ["差出人"]')
        sys.exit(1)
    company = sys.argv[1]
    situation = sys.argv[2]
    sender = sys.argv[3] if len(sys.argv) > 3 else ""
    print(generate(company, situation, sender))


if __name__ == '__main__':
    main()
