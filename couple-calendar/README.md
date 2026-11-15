# 夫婦のスケジュール・コンシェルジュ

夫婦で使う **Googleの共有カレンダー** を、そのまま予定の保管場所として使います。
その上に入り口を2つ用意しました。

- **Claude Code** から「来週土曜の18時から、ふたりで焼肉を入れて」と頼む（MCPサーバー）
- **LINE** でコンシェルジュと会話して、予定の確認・登録・変更・空き時間探しをする（Webhookサーバー）

スマホのGoogleカレンダーアプリからも、これまでどおり直接予定を入れられます。どの入り口から入れても、同じ1つのカレンダーに集まります。

```
 Claude Code ──(MCP)──┐
                      ├──> 共通のカレンダー操作 (tools.py) ──> Google 共有カレンダー <── カレンダーアプリ
 LINE ──> Cloud Run ──┘        ↑
          (Webhook)       Claude API（コンシェルジュ）
```

## できること

| 機能 | 例 |
| --- | --- |
| 予定の確認 | 「今週の予定は？」「はなこの来月の予定を教えて」 |
| 登録 | 「金曜19時から21時、ふたりでディナー」「23日は結婚記念日（終日）」「毎週火曜7:30にゴミ出し」 |
| 変更・削除 | 「ディナーを土曜に変えて」「歯医者キャンセルになった」 |
| 空き時間探し | 「来週、ふたりとも空いてる夜は？」（各自の個人カレンダーも見られます） |
| 朝のお知らせ（任意） | 毎朝7時に今日と明日の予定をLINEに送る |

予定には **担当者**（夫・妻・ふたり）が付きます。担当者ごとに予定の色が変わるので、カレンダーアプリでもひと目で分かります。カレンダーアプリから直接入れた予定も、色か作成者のアカウントで担当者を見分けます。

LINEでは、1対1のトークでも、夫婦＋ボットのグループトークでも使えます。グループでは「コンシェルジュ、〜」と呼びかけるか、ボットをメンションしたときだけ答えます。

---

## セットアップ

所要時間の目安は1時間ほどです。手順1〜2でClaude Codeから使えるようになり、手順3〜5でLINEのコンシェルジュが動きます。

### 1. Googleカレンダーの準備

#### 1-1. Google Cloud プロジェクトとサービスアカウント

プログラムがカレンダーを読み書きするための「ロボット用のGoogleアカウント」（サービスアカウント）を作ります。

```bash
PROJECT_ID=my-couple-calendar   # 好きなIDに
gcloud projects create $PROJECT_ID   # 既存のプロジェクトを使うなら不要
gcloud config set project $PROJECT_ID
gcloud services enable calendar-json.googleapis.com

gcloud iam service-accounts create couple-calendar --display-name "夫婦カレンダー"
SA=couple-calendar@${PROJECT_ID}.iam.gserviceaccount.com
echo $SA   # このメールアドレスを次の手順で使う
```

#### 1-2. 共有カレンダーを作って共有する

1. パソコンのGoogleカレンダーで、左の「他のカレンダー」の「＋」→「新しいカレンダーを作成」（名前は「夫婦」など）
2. 作ったカレンダーの「設定と共有」→「特定のユーザーまたはグループと共有する」で、次の2人を追加
   - 配偶者のGoogleアカウント … 権限「予定の変更」
   - 上のサービスアカウントのメールアドレス（`$SA`） … 権限「予定の変更」
3. 同じ画面の「カレンダーの統合」にある **カレンダーID**（`xxxx@group.calendar.google.com`）を控える

> すでに二人で使っている共有カレンダーがあれば、それにサービスアカウントを追加するだけで大丈夫です。

#### 1-3. （任意）個人カレンダーの空き時間も見る

空き時間探しで、それぞれの個人カレンダー（仕事の予定など）も考慮したい場合は、各自の個人カレンダーを
サービスアカウントに **「予定の表示（時間枠のみ、詳細は非表示）」** で共有し、`.env` の
`MEMBERx_PERSONAL_CALENDAR_ID` にそのGmailアドレスを入れます。中身（タイトルなど）は見えず、「埋まっている時間」だけが使われます。

### 2. Claude Code から使う

```bash
cd couple-calendar
cp .env.example .env   # GOOGLE_CALENDAR_ID、二人の名前、MCP_USER_NAME などを記入

# ローカル用の鍵ファイル（このファイルはパスワードと同じ扱いで。git には入りません）
mkdir -p ~/.config/couple-calendar
gcloud iam service-accounts keys create ~/.config/couple-calendar/sa-key.json --iam-account=$SA
```

Claude Code に登録します。`--scope user` にしておくと、どのフォルダでClaude Codeを開いても使えます。

```bash
claude mcp add couple-calendar --scope user -- uv run --directory "$(pwd)" couple-calendar-mcp
```

（このリポジトリを開いたときだけ使うなら、リポジトリ直下の `.mcp.json` がそのまま使えます。）

Claude Code で話しかけてみてください。

```
> 来週土曜の18時から、ふたりで焼肉を入れておいて
> 今月、はなこが空いてる平日の夜は？
> 歯医者の予定を30分後ろにずらして
```

夫婦それぞれがClaude Codeを使う場合は、各自のパソコンで同じ手順を行い、`MCP_USER_NAME` を自分の名前にします。

### 3. LINE公式アカウント（Messaging API）を作る

1. [LINE Official Account Manager](https://manager.line.biz/) で公式アカウントを作り、「設定」→「Messaging API」で Messaging API を有効にする
2. [LINE Developers コンソール](https://developers.line.biz/console/) で、そのチャネルの
   - 「チャネル基本設定」→ **チャネルシークレット**
   - 「Messaging API設定」→ **チャネルアクセストークン（長期）** を発行
   を控える
3. LINE Official Account Manager の「応答設定」で
   - 応答メッセージ: オフ（定型文の自動返信を止める）
   - Webhook: オン
4. グループで使う場合は「Messaging API設定」→「グループトーク・複数人トークへの参加を許可する」をオン

あわせて [Claude Console](https://console.anthropic.com/) で **APIキー** を発行しておきます。

### 4. Cloud Run にデプロイする

```bash
REGION=asia-northeast1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com

# 秘密情報は Secret Manager へ（値は自分のものに置き換え）
printf '%s' 'sk-ant-...'               | gcloud secrets create anthropic-api-key --data-file=-
printf '%s' 'チャネルシークレット'        | gcloud secrets create line-channel-secret --data-file=-
printf '%s' 'チャネルアクセストークン'    | gcloud secrets create line-channel-access-token --data-file=-
printf '%s' "$(openssl rand -hex 24)"  | gcloud secrets create cron-secret --data-file=-
for s in anthropic-api-key line-channel-secret line-channel-access-token cron-secret; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done

# 秘密情報以外の設定
cp deploy/env.example.yaml deploy/env.yaml   # カレンダーIDと二人の名前を記入

gcloud run deploy couple-concierge \
  --source . --region $REGION \
  --service-account $SA \
  --allow-unauthenticated \
  --max-instances 1 --timeout 120 \
  --env-vars-file deploy/env.yaml \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest,LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-access-token:latest,CRON_SECRET=cron-secret:latest
```

- Cloud Run ではサービスアカウントがそのまま使われるので、鍵ファイルは要りません。
- `--allow-unauthenticated` はLINEから呼べるようにするためです。LINEの署名を検証し、夫婦以外のLINEユーザーには応答しないので、他人がカレンダーを操作することはできません。
- `--max-instances 1` は、会話の流れをサーバーのメモリに覚えているためです（複数台に分かれると話がつながらなくなります）。

### 5. LINEとつなぐ

1. デプロイ後に表示されたURLの末尾に `/callback` を付けて、LINE Developers の「Webhook URL」に設定し「検証」→ 成功を確認、「Webhookの利用」をオン
2. 夫婦それぞれがボットを友だち追加し、何か送る → **ユーザーID**（`U...`）が返ってくる（セットアップモード）
3. `deploy/env.yaml` の `MEMBER1_LINE_USER_ID` / `MEMBER2_LINE_USER_ID` に入れ、`LINE_SETUP_MODE: "false"` にして、手順4の `gcloud run deploy` をもう一度実行

これで完成です。LINEで「今週の予定は？」と送ってみてください。

### 6. （任意）朝のお知らせ

毎朝、今日と明日の予定をLINEに送ります。

1. 送り先のIDを調べる: 1対1ならユーザーID、グループなら、そのグループで「ID」と送ると返ってくる「このトークのID」（`C...`）
2. `deploy/env.yaml` の `DAILY_BRIEF_TO` に入れて再デプロイ
3. Cloud Scheduler を設定

```bash
gcloud services enable cloudscheduler.googleapis.com
URL=$(gcloud run services describe couple-concierge --region $REGION --format 'value(status.url)')
gcloud scheduler jobs create http couple-daily-brief \
  --location $REGION --schedule "0 7 * * *" --time-zone "Asia/Tokyo" \
  --uri "$URL/tasks/daily-brief" --http-method POST \
  --headers "X-Cron-Secret=$(gcloud secrets versions access latest --secret cron-secret)"
```

---

## LINEでの使い方のコツ

- 「私」「自分」は送った人のこと、と理解します。「ふたりで」「一緒に」は二人の予定になります。
- 話の流れは30分覚えています（「それ19時に変えて」が通じます）。話題を変えたいときは「リセット」と送ります。
- 「ID」と送ると、自分のユーザーIDとトークのIDを返します。

## 料金の目安

- **LINE**: 無料プランで使えます。コンシェルジュの返信（応答メッセージ）は無料の通数に数えられません。朝のお知らせはプッシュ送信なので数に入ります（無料プランは月200通。2人のグループに送ると1回で2通分）。
- **Cloud Run / Secret Manager / Cloud Scheduler**: 夫婦で使う程度なら、ほぼ無料枠に収まります。
- **Claude API**: やりとり1回ごとに使った分だけかかります。安くしたいときは `CLAUDE_EFFORT=low` にするか、`CLAUDE_MODEL` をより安いモデルに変えてください。

## ファイル構成

```
couple-calendar/
├── src/couple_calendar/
│   ├── config.py      設定（.env / 環境変数）
│   ├── gcal.py        Googleカレンダーの読み書き、担当者の判定、空き時間の計算
│   ├── tools.py       カレンダー操作ツール（Claude Code と LINE で共通）
│   ├── concierge.py   LINEのコンシェルジュ（Claude API + ツール）
│   ├── line_app.py    LINEのWebhookサーバー（FastAPI）
│   └── mcp_server.py  Claude Code用のMCPサーバー
├── tests/             テスト（Googleカレンダー・Claude API・LINEはモックで確認）
├── deploy/env.example.yaml
├── Dockerfile
└── .env.example
```

## 開発

```bash
uv run --extra dev pytest
```

## うまくいかないとき

| 症状 | 確認すること |
| --- | --- |
| 「カレンダーへのアクセス権がありません」 | 共有カレンダーの共有設定に、サービスアカウントのメールアドレスが「予定の変更」で入っているか |
| 「予定が見つかりませんでした」 | `GOOGLE_CALENDAR_ID` が共有カレンダーのIDになっているか（`primary` などではなく） |
| LINEで返事がない | Webhookがオンか、応答メッセージがオフか、`MEMBERx_LINE_USER_ID` が正しいか。Cloud Run のログを確認 |
| グループで返事がない | 「コンシェルジュ、〜」で始めるか、ボットをメンションしているか |
| 個人カレンダーの空き時間が反映されない | 個人カレンダーをサービスアカウントに共有したか（返信に「参照できませんでした」と出ます） |
