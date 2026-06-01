# Writing Robot 営業ツール

手書きDMによる法人営業を自動化するプロジェクト。
会社を選ぶと、情報を収集し、刺さる文面を設計し、ロボット（Writing Robot T-A4）が
手書きで便箋を書き起こす。

## これは何か

- **利用者**：チーム5人が各自で使う
- **ターゲット**：中小企業を幅広く（数を打つ）
- **想定量**：1日 10〜50通
- **用途**：法人営業の手書きDM

## ドキュメント

| ファイル | 内容 |
|---|---|
| [ROADMAP.md](./ROADMAP.md) | 全体設計・アーキテクチャ・開発フェーズ・進捗 |
| [SETUP.md](./SETUP.md) | VS Code での環境構築〜直線テストまでの手順 |
| [tests/test_write.py](./tests/test_write.py) | 書字品質テスト用スクリプト（Phase 1） |

## いまの状況

- **Phase 0（ロボット制御の基盤）**：完了 ✅
- **Phase 1（書字品質の検証）**：進行中 ◀

詳細と次の一手は [ROADMAP.md](./ROADMAP.md) を参照。

## クイックスタート

```bash
# 1. 必要なライブラリを入れる
pip install pyserial

# 2. ロボットの電源を入れて USB 接続

# 3. 直線テストを走らせる
python tests/test_write.py 1
```

詳しくは [SETUP.md](./SETUP.md) を上から順に。
