# daily-paper-coach

論文を3日かけて読み、毎朝1問ずつ理解度確認クイズに答える学習自動化システム。PMSMセンサレス制御を研究者レベルまで段階的に積み上げることを目的とする。

詳細な要件は [論文学習システム要件定義書.md](./論文学習システム要件定義書.md) を参照（下記のとおり運用形態は要件定義書から見直している）。

> 変更点: (1) スケジュール実行がベストエフォートで信頼性が下がるため夜ジョブを廃止し**朝ジョブ1本**に統合。(2) 1日で論文1本＋3問は負荷が高いため、**論文は3日に1本・出題は毎日1問**（Day1=Q1課題／Day2=Q2手法／Day3=Q3進歩性）の3日サイクルに変更。

## 構成

```
.github/workflows/morning.yml   # 朝ジョブ (JST 7:00 / 7:40 の2枠)
src/morning.py                  # 採点・ペナルティ・論文選定/配信・毎日の出題
src/lib/
  config.py                     # 環境変数ベースの設定
  discord.py                    # 投稿(Webhook) / 履歴取得(Bot API)
  s2.py                         # 論文検索(Semantic Scholar/OpenAlex/arXiv)・PDF取得
  beeminder.py                  # 未回答時ペナルティ
  claude.py                     # 論文選定・要約・出題・採点・原因推定
  store.py                      # JSON読み書き・git commit
data/
  roadmap.json                  # フェーズ定義・現在位置・候補
  log.json                      # 学習ログ（1問ごと）
  state.json                    # 実行状態: last_morning_date（冪等化）/ delivered（既読）
                                #   / active（学習中の論文と3日サイクルの進捗）/ queue（未消化論文）
```

## 日次フロー（3日サイクル）

| | 朝の動作 |
|---|---|
| Day 1 | 前サイクルQ3を採点 → 新しい論文を配信（3日間の読書プラン付き）→ Q1（課題把握）を出題 |
| Day 2 | 前日Q1を採点 → Q2（手法理解）を出題 |
| Day 3 | 前日Q2を採点 → Q3（進歩性）を出題 |

- 論文は3日に1本、出題は毎日1問、採点も毎朝（未回答ならペナルティ、回答すれば Beeminder 1点）
- 未消化の論文は `queue` から新規検索より優先して消化する
- 期限は各問「翌朝7:00の採点まで」

信頼性対策: 朝ジョブは JST 7:00 と 7:40 の2枠で走らせ、片方がスキップされても救済する。採点は配信の前に確定コミットするため、二重実行しても採点・ペナルティは一度だけ。

## セットアップ

要件定義書の §11 に沿って以下を用意し、GitHub リポジトリの Secrets に登録する。

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
2. `morning` workflow を手動実行 → 論文配信とクイズ出題を確認
3. Discord で `A1: ... / A2: ... / A3: ...`（冒頭に `[読了]` 等）を返信
4. 翌日、`morning` を再実行 → 採点結果＋次の論文配信を確認
   （同日中は冪等ガードによりスキップされるため、採点確認は翌日か、`data/state.json` を消してから）
5. 問題なければ `DRY_RUN_PENALTY` を削除（または `0`）

## Discord コマンド（次のジョブで反映）

- `!skip` : 今日は休み。ペナルティ対象外、同じ論文を翌日に持ち越す
- `!request <テーマ>` : 次の論文のテーマ希望
- `!feedback <内容>` : 難易度・分量などへの要望

## ローカル実行

環境変数を設定した上で:

```bash
pip install -r requirements.txt
cd src
python morning.py
```
