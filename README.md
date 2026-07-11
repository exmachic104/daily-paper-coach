# daily-paper-coach

毎日1本・約15分で読める論文を配信し、夜に理解度確認クイズを出題、翌朝に自動採点する学習自動化システム。PMSMセンサレス制御を研究者レベルまで段階的に積み上げることを目的とする。

詳細な要件は [REQUIREMENTS.md](./REQUIREMENTS.md) を参照。

## 構成

```
.github/workflows/morning.yml   # JST 7:00  (cron: '0 22 * * *')
.github/workflows/evening.yml   # JST 21:00 (cron: '0 12 * * *')
src/morning.py                  # 朝ジョブ: 採点・ペナルティ・論文選定・配信・出題事前生成
src/evening.py                  # 夜ジョブ: 出題の投稿
src/lib/
  config.py                     # 環境変数ベースの設定
  discord.py                    # 投稿(Webhook) / 履歴取得(Bot API)
  s2.py                         # 論文検索(Semantic Scholar/OpenAlex/arXiv)・PDF取得
  beeminder.py                  # 未回答時ペナルティ
  claude.py                     # 論文選定・要約・出題・採点・原因推定
  store.py                      # JSON読み書き・git commit
data/
  roadmap.json                  # フェーズ定義・現在位置・候補
  log.json                      # 学習ログ
  pending_quiz.json             # 出題ストック(朝に生成→夜に投稿→翌朝採点後に削除)
```

## 日次フロー

- **朝 (JST 7:00)**: 前夜の出題への回答を採点 → Beeminder 判定 → 学習ログ更新 → 次の論文を選定・配信 → 夜の出題を事前生成
- **夜 (JST 21:00)**: 事前生成した3問を Discord に投稿

## セットアップ

REQUIREMENTS.md の §11 に沿って以下を用意し、GitHub リポジトリの Secrets に登録する。

| Secret | 内容 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic APIキー |
| `DISCORD_WEBHOOK_URL` | 投稿用 Webhook URL |
| `DISCORD_BOT_TOKEN` | 履歴読み取り用 Bot Token（Message Content Intent 必須） |
| `DISCORD_CHANNEL_ID` | 対象チャンネルID |
| `BEEMINDER_USERNAME` | Beeminder ユーザー名 |
| `BEEMINDER_AUTH_TOKEN` | Beeminder APIトークン |
| `BEEMINDER_GOAL` | ゴール名（例: `paper-quiz`） |
| `USER_EMAIL` | OpenAlex polite pool 用メール |

補足（任意、Repository variables として設定可）:

- `ANTHROPIC_MODEL`: 使用モデル（既定 `claude-opus-4-8`）
- `DRY_RUN_PENALTY`: `1` の間は Beeminder 送信を無効化（通しテスト用）

Settings → Actions → General → Workflow permissions を **Read and write permissions** にすること（ログの commit に必要）。

## 通しテスト手順

1. `DRY_RUN_PENALTY=1` を設定
2. `morning` workflow を手動実行 → 配信を確認
3. `evening` workflow を手動実行 → 出題を確認
4. Discord で `A1: ... / A2: ... / A3: ...`（冒頭に `[読了]` 等）を返信
5. 再度 `morning` を手動実行 → 採点結果を確認
6. 問題なければ `DRY_RUN_PENALTY` を削除（または `0`）

## Discord コマンド（次のジョブで反映）

- `!skip` : 今日は休み。ペナルティ対象外、同じ論文を翌日に持ち越す
- `!request <テーマ>` : 次の論文のテーマ希望
- `!feedback <内容>` : 難易度・分量などへの要望

## ローカル実行

環境変数を設定した上で:

```bash
pip install -r requirements.txt
cd src
python morning.py   # または python evening.py
```
