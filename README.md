# daily-paper-coach

毎日1本・約15分で読める論文を配信し、同時に理解度確認クイズを出題、翌朝に自動採点する学習自動化システム。PMSMセンサレス制御を研究者レベルまで段階的に積み上げることを目的とする。

詳細な要件は [論文学習システム要件定義書.md](./論文学習システム要件定義書.md) を参照。

> 注: 要件定義書は「朝に配信・夜に出題」の二段構えだが、GitHub Actions のスケジュール実行がベストエフォート（遅延・スキップあり）で、1日2回の成功に依存すると信頼性が下がるため、**朝ジョブ1本に統合**し論文とクイズを同時配信する構成に変更している。

## 構成

```
.github/workflows/morning.yml   # 朝ジョブ (JST 7:00 / 7:40 の2枠)
src/morning.py                  # 採点・ペナルティ・論文選定・配信・クイズ出題
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
  pending_quiz.json             # 出題ストック(朝に生成・投稿→翌朝採点後に削除)
  state.json                    # 実行状態(最後に完了した朝の日付。二重実行の冪等化用)
```

## 日次フロー

- **朝 (JST 7:00)**: 前日の出題への回答を採点 → Beeminder 判定 → 学習ログ更新 → 次の論文を選定・配信 →（同時に）理解度確認クイズ3問を出題
- ユーザーはその日のうちに論文（指定セクション）を読み、Discord に回答
- 翌朝の採点まで（期限 JST 7:00）

信頼性対策: 朝ジョブは JST 7:00 と 7:40 の2枠で走らせ、片方がスキップされても救済する。`data/state.json` に完了日を記録し、同日の二重実行は冪等にスキップされる。

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
