# 論文毎日学習システム 要件定義書

作成日: 2026-07-10
実装方法: Claude Code によるスクラッチ実装
リポジトリ名（推奨）: `daily-paper-coach`

---

## 1. 目的と概要

利用者（以下ユーザー）が毎日1本、約15分で読める論文（または論文の指定セクション）を読み、その日の夜に出題される理解度確認クイズに答えることで、特定分野の知識を研究者レベルまで段階的に積み上げるための自動化システム。

- 毎朝、AIが学習ログとロードマップに基づいて論文を1本選定し、Discordに配信する
- 毎晩、その論文に関する短答式の質問を3問出題する
- 翌朝までに回答がなければペナルティ（Beeminder経由の金銭ペナルティ）を科す
- 回答は自動採点し、誤答の場合は「誤解の原因」を推定してフィードバックする
- 学習ログを蓄積し、論文選定の難易度・範囲を動的に調整する

## 2. システム構成

| 要素 | 採用技術 | 役割 |
|---|---|---|
| スケジューラ | GitHub Actions (cron) | 朝ジョブ・夜ジョブの定期実行 |
| 配信・回答チャネル | Discord | 論文配信、出題、ユーザーの回答受付 |
| 投稿手段 | Discord Webhook | ジョブからチャンネルへの投稿 |
| 回答取得手段 | Discord Bot (REST API) | チャンネルのメッセージ履歴取得（常駐不要、ジョブ実行時にHTTPで読むだけ） |
| AI | Anthropic API (Claude) | 論文選定・要約・出題・採点・原因推定 |
| 論文検索 | Semantic Scholar API（主・キーなし可）+ OpenAlex API（フォールバック）+ arXiv API（補助） | オープンアクセスPDFのある論文の検索・メタデータ取得 |
| ペナルティ | Beeminder API | 未回答時のデータポイント欠落による自動課金 |
| データ保存 | リポジトリ内のJSON/Markdownファイル（Actionsからgit commit） | 学習ログ、ロードマップ、出題ストック |

### 2.1 実行言語・環境

- Python 3.11+ を推奨（requests / anthropic SDK のみで実現可能。重い依存は避ける）
- GitHub Actions の `ubuntu-latest` ランナーで動作すること
- ローカル（Windows）でも手動実行できるよう、環境変数ベースの設定とする

### 2.2 タイムゾーン

- ユーザーはJST（Asia/Tokyo）。GitHub Actionsのcronは**UTC指定**なので変換に注意
- 朝ジョブ: JST 7:00（UTC 22:00 前日）
- 夜ジョブ: JST 21:00（UTC 12:00）
- GitHub Actionsのcronは最大15〜30分程度遅延しうる。時刻厳密性は要求しないが、回答期限の判定はメッセージのタイムスタンプで行うこと

## 3. 日次フロー

### 3.1 朝ジョブ（JST 7:00）

1. **回答チェック**: Discord Bot APIで対象チャンネルの履歴を取得し、前夜の出題メッセージ以降のユーザー投稿を回答として収集する
2. **採点**: 回答が存在する場合、Claude APIに「論文の要約・出題・模範解答・ユーザー回答・自己申告の読了状況」を渡して採点。各問について正誤と、誤答の場合は原因推定（後述 §5）を生成し、Discordに投稿する
3. **ペナルティ判定**: 
   - 回答があった場合: Beeminderにデータポイント（value=1）を送信 → 目標達成
   - 回答がなかった場合: データポイントを送信しない → Beeminder側で自動的にderail（課金）が発生。Discordに「未回答のためペナルティが発生します」と通知する
4. **学習ログ更新**: `data/log.json` に当日分のレコードを追記し、git commit & push する
5. **次の論文選定**: 学習ログとロードマップ（§6）をClaude APIに渡し、次に読むべき論文の検索クエリ方針を生成 → Semantic Scholar APIで検索 → オープンアクセスPDFが存在する候補から Claude が1本選定
6. **配信メッセージ生成**: 選定した論文のPDFを取得し、Claude APIに渡して以下を生成、Discordに投稿する
   - タイトル・著者・年・PDFへの直リンク
   - 3〜4文の日本語要約（何が課題で、何を提案し、何が新しいか）
   - 「今日の読みどころ」: 15分で読むための指示（読むべきセクション、飛ばしてよい箇所、注目すべき図表）
   - ロードマップ上の位置づけ（例: 「フェーズ2: モデルベース手法 3/5本目」）
7. **出題の事前生成**: 同時に夜の質問3問と模範解答を生成し、`data/pending_quiz.json` に保存する（この時点ではDiscordに投稿しない）

### 3.2 夜ジョブ（JST 21:00）

1. `data/pending_quiz.json` を読み、質問3問をDiscordに投稿する
2. 回答フォーマットの案内を毎回付記する:
   - 「A1: ... / A2: ... / A3: ... の形式で、このチャンネルに返信してください」
   - 「冒頭に読了状況を1つ添えてください: `[読了]` `[途中]` `[未読]`」
   - 「期限: 明朝7:00の採点まで」

### 3.3 エラー時の挙動

- 論文検索・PDF取得・API呼び出しの失敗時は、エラー内容をDiscordに投稿する（無言で落ちない）
- PDF取得失敗時は次点候補に自動フォールバック（最大3候補）
- 夜ジョブで `pending_quiz.json` が無い場合は「本日の配信に失敗していたため出題はありません。ペナルティ対象外です」と投稿し、翌朝のペナルティ判定をスキップするフラグを立てる

## 4. 出題仕様

- 各論文につき3問。短い文章（1〜3文）で答えられる形式
- 3問の設計方針を固定する:
  - **Q1（論点把握）**: この論文が解決しようとした課題は何か
  - **Q2（手法理解）**: 提案手法の核となるアイデア・原理は何か
  - **Q3（進歩性）**: 従来手法と比べて何がどう改善されたか／どんな限界が残るか
- 要約を読んだだけでは答えられず、本文（指定セクション）を読んでいれば答えられる粒度にする
- 出題・採点・フィードバックはすべて日本語

## 5. 採点と誤答原因の推定

採点時、Claude APIへのプロンプトに以下を含める:

- 論文の要約と該当セクションの内容（朝ジョブで抽出しキャッシュしたもの）
- 各問の模範解答
- ユーザーの回答と自己申告の読了状況（`[読了]` / `[途中]` / `[未読]`）
- 直近2週間の学習ログ（誤答傾向の文脈として）

出力させる内容:

1. 各問の判定（正解 / 部分的に正解 / 不正解）と簡潔な解説
2. 誤答があった場合の原因分類:
   - **時間不足**: 該当箇所まで読めていない（読了状況の申告と誤答箇所の対応から判定）
   - **概念の誤解**: 読んだが原理を取り違えている（回答内容に誤った理解の痕跡がある）
   - **前提知識の不足**: 論文以前の基礎概念でつまずいている（この場合、補うべき基礎トピックを提示する）
   - **問題の読み違え**: 理解はしているが問いとずれた回答をしている
3. 原因に応じた翌日以降への反映提案（例: 「明日は同テーマの易しめの論文にする」「基礎解説を配信に追加する」）

この原因分類は学習ログに記録し、論文選定に反映する。

## 6. ロードマップと論文選定

### 6.1 初期ロードマップ（PMSMセンサレス制御・15本）

`data/roadmap.json` に以下を初期データとして持つ:

- **フェーズ1: 全体像（1〜2本）**
  - サーベイ論文から開始。初日候補: "A Review of Position Sensorless Compound Control for PMSM Drives" (World Electric Vehicle Journal, MDPI, 2023, オープンアクセス)
  - 長いサーベイは2日に分割し、日ごとに読むセクションを指定する
- **フェーズ2: 中高速域のモデルベース手法（4〜5本）**
  - 拡張誘起電圧（EEMF）、磁束オブザーバ、スライディングモードオブザーバ（SMO）の代表論文・改良論文
- **フェーズ3: 低速・零速域（3〜4本）**
  - 高周波重畳法（HFI）、I/F始動、突極性利用の原理
- **フェーズ4: 全速度域複合制御と最新動向（3〜4本）**
  - 速度域間の切替手法、適応オブザーバ、AI応用（2023年以降）

### 6.2 選定ロジック

- 必須条件: オープンアクセスPDFが存在すること（Semantic Scholarの `openAccessPdf` フィールド、OpenAlex使用時は `open_access.oa_url` フィールドで判定）
- Semantic Scholar APIは**キーなし**で使用する。429（レート制限）が返った場合は指数バックオフで数回リトライし、それでも失敗する場合はOpenAlex API（キー不要。`mailto` パラメータにユーザーのメールアドレスを付けてpolite poolを利用）にフォールバックする
- 15分で読める分量になるよう、長い論文は読むセクションを指定して分割する
- 学習ログの正答率・原因分類に応じて調整:
  - 正答率が高く安定 → フェーズを進める／やや難しい論文へ
  - 「前提知識の不足」が続く → 基礎寄りの論文・教科書的サーベイを挿入
  - 「時間不足」が続く → 読む範囲を狭める
- 既読論文は `data/log.json` で管理し、重複選定しない
- フェーズ4完了後は、ログをもとにClaudeが次のサブ分野候補を2〜3提案し、Discordでユーザーに選ばせる

### 6.3 ユーザーからの操作

Discordチャンネルへの特定プレフィックス付き投稿をジョブ実行時に解釈する（常駐Bot不要、次のジョブで反映されれば十分）:

- `!skip` : 今日は休み。ペナルティ対象外とし、同じ論文を翌日に持ち越す
- `!request <テーマ>` : 次の論文のテーマ希望
- `!feedback <内容>` : 難易度・分量などへの要望（選定プロンプトに反映）

## 7. データ設計

すべてリポジトリ内で管理し、Actionsからcommitする。

### 7.1 `data/log.json`（学習ログ）

```json
[
  {
    "date": "2026-07-13",
    "paper": {
      "title": "...",
      "s2_paper_id": "...",
      "url": "...",
      "phase": 2,
      "assigned_sections": "Sec.1-3"
    },
    "answered": true,
    "self_reported_status": "読了",
    "results": [
      {"q": 1, "verdict": "correct"},
      {"q": 2, "verdict": "incorrect", "cause": "概念の誤解", "note": "..."},
      {"q": 3, "verdict": "partial", "cause": "時間不足", "note": "..."}
    ],
    "penalty": false
  }
]
```

### 7.2 `data/roadmap.json`

フェーズ定義、現在位置、各フェーズの候補論文リスト（選定済み・未選定）。

### 7.3 `data/pending_quiz.json`

当日の論文情報、質問3問、模範解答、採点用に抽出した本文要点。夜ジョブが投稿し、翌朝ジョブが採点に使用後、ログへ移して削除。

## 8. Discordメッセージ仕様

- 朝の配信・夜の出題・採点結果はEmbed形式で見やすく（スマホでの可読性優先）
- 論文PDFへの直リンクを必ず含める（スマホのブラウザでそのまま開けること）
- 採点結果には次回への一言アドバイスを含める
- 1メッセージ2000文字制限に注意し、必要に応じて分割する

## 9. Beeminder連携仕様

- ゴールタイプ: Do More（1日1データポイント必須）
- ゴール名（例）: `paper-quiz`
- 回答があった朝のみ `POST /users/{user}/goals/paper-quiz/datapoints.json` で value=1 を送信
- `!skip` 使用日と配信失敗日は value=1 を送信して免除する（Beeminderの休暇設定に頼らずシステム側で制御）
- deadline はBeeminder側でJST朝7:00に合わせて設定する（事前準備手順参照）

## 10. GitHub Secrets（環境変数）一覧

| Secret名 | 内容 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic APIキー |
| `DISCORD_WEBHOOK_URL` | 投稿用WebhookのURL |
| `DISCORD_BOT_TOKEN` | 履歴読み取り用BotのToken |
| `DISCORD_CHANNEL_ID` | 対象チャンネルのID |
| `BEEMINDER_USERNAME` | Beeminderのユーザー名 |
| `BEEMINDER_AUTH_TOKEN` | BeeminderのAPIトークン |
| `BEEMINDER_GOAL` | ゴール名（例: paper-quiz） |
| `USER_EMAIL` | OpenAlexのpolite pool用メールアドレス（Gmail可） |

## 11. 事前準備手順（ユーザー作業）

### 11.1 GitHubリポジトリ

1. GitHubでプライベートリポジトリ `daily-paper-coach` を作成する
2. Settings → Actions → General → Workflow permissions を「Read and write permissions」にする（ログのcommitに必要）

### 11.2 Discord

1. Discordで自分専用サーバーを作成し、チャンネル `#papers` を作る
2. **Webhook作成**: チャンネル設定 → 連携サービス → ウェブフック → 新しいウェブフック → URLをコピー → `DISCORD_WEBHOOK_URL` へ
3. **Bot作成**（履歴読み取り用）:
   1. https://discord.com/developers/applications で New Application
   2. Bot タブ → Reset Token でTokenを取得 → `DISCORD_BOT_TOKEN` へ
   3. 同じBotタブで「Message Content Intent」をONにする（メッセージ本文の取得に必須）
   4. OAuth2 → URL Generator で scope: `bot`、権限: `View Channels`, `Read Message History` を選び、生成されたURLで自分のサーバーに招待
4. **チャンネルID取得**: Discordの設定 → 詳細設定 → 開発者モードをON → `#papers` を右クリック → 「IDをコピー」→ `DISCORD_CHANNEL_ID` へ
5. スマホのDiscordアプリで `#papers` の通知をONにする

### 11.3 Anthropic API

1. https://console.anthropic.com でAPIキーを発行 → `ANTHROPIC_API_KEY` へ
2. 使用量の目安: 1日2ジョブ・PDF読み込み込みで概ね数十円〜/日程度。Consoleで月額上限を設定しておくと安心

### 11.4 Beeminder

1. https://www.beeminder.com でアカウント作成
2. New Goal → 「Do More」タイプ → 名前 `paper-quiz`、単位 `answers`、レート「1 per day」で作成
3. ゴール設定の Deadline を朝7:00（JST）に設定する
4. https://www.beeminder.com/api/v1/auth_token.json にログイン状態でアクセスし、表示される `auth_token` を控える → `BEEMINDER_AUTH_TOKEN` へ
5. ユーザー名 → `BEEMINDER_USERNAME` へ
6. クレジットカードを登録する（これが無いとペナルティが機能しない）。初期ペナルティ額は$5から始まり、derailのたびに段階的に上がるのがデフォルト

### 11.5 論文検索API

- Semantic Scholar APIは**キーなし**で使用するため、申請作業は不要
- OpenAlex APIもキー不要。polite pool用にメールアドレス（Gmail可）を `USER_EMAIL` としてSecretsに登録するだけでよい

### 11.6 Secrets登録

リポジトリの Settings → Secrets and variables → Actions → New repository secret で §10 の一覧をすべて登録する。

具体的な手順:

1. 「New repository secret」を押すと Name / Secret の2つの入力欄が表示される
2. **Name** には §10 の表の左列の名前をそのまま入力する（例: `ANTHROPIC_API_KEY`）。大文字・小文字・アンダースコアまで完全一致させること。コードはこの名前で値を参照するため、不一致だと動作しない
3. **Secret** には実際の値（APIキーやURL）だけを貼り付ける。引用符や前後のスペースは不要
4. 「Add secret」で1件登録完了。§10 の件数分（全8件）繰り返す

注意点:

- 登録後は値を再表示できない（セキュリティ仕様）。修正したい場合は該当Secretの「Update」で上書きすればよい
- `DISCORD_WEBHOOK_URL` は `https://discord.com/api/webhooks/...` で始まるURL全体を貼る
- `DISCORD_CHANNEL_ID` は数字のみの文字列
- Secretsの値はActionsの実行ログ上で自動的に `***` にマスクされる

## 12. Claude Codeへの実装指示メモ

- 本書をリポジトリ直下に `REQUIREMENTS.md` として置き、Claude Codeに読ませてから実装を開始すること
- 推奨ファイル構成:
  ```
  .github/workflows/morning.yml   # JST7:00 (cron: '0 22 * * *')
  .github/workflows/evening.yml   # JST21:00 (cron: '0 12 * * *')
  src/morning.py
  src/evening.py
  src/lib/discord.py / s2.py / beeminder.py / claude.py / store.py
  data/roadmap.json / log.json
  REQUIREMENTS.md
  ```
- 両workflowに `workflow_dispatch` を付け、手動実行でテストできるようにすること
- 最初の動作確認手順: (1) 手動で朝ジョブを実行し配信を確認 → (2) 手動で夜ジョブを実行し出題を確認 → (3) Discordで回答を投稿 → (4) 再度朝ジョブを実行し採点を確認、の順で通しテストを行う
- Beeminder連携は通しテストが終わるまで環境変数 `DRY_RUN_PENALTY=1` で無効化できるようにすること
- PDFはClaude APIにdocumentブロック（base64）で渡す。サイズ超過時（32MB/100ページ超）はテキスト抽出にフォールバックすること
