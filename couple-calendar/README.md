# りまいんたろう — 夫婦のスケジュール・コンシェルジュ

夫婦で使う **Googleの共有カレンダー** を、そのまま予定の保管場所として使います。
その上に入り口を2つ用意しました。

- **Claude Code** から「来週土曜の18時から、ふたりで焼肉を入れて」と頼む（MCPサーバー）
- **LINE** でコンシェルジュの「りまいんたろう」と会話して、予定の確認・登録・変更・空き時間探しをする（Webhookサーバー）

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
| 画像から登録 | 学校のお便り・チラシ・招待状・予約完了画面のスクショを送ると、日時を読み取って登録 |
| 文章から登録 | 予定が書かれたメールやメッセージを貼り付けて送ると、そこから登録 |
| 仕事の出張・移動 | 仕事のカレンダーから出張・移動だけを3日に1回自動で写す。「仕事の出張を入れて」と頼めばすぐ写す（任意） |

予定には **担当者**（夫・妻・ふたり）が付きます。担当者ごとに予定の色が変わるので、カレンダーアプリでもひと目で分かります。カレンダーアプリから直接入れた予定も、色か作成者のアカウントで担当者を見分けます。

LINEでは、1対1のトークでも、夫婦＋ボットのグループトークでも使えます。

- **1対1のトーク**: テキストや画像を送ると、すぐに答えます。画像を何枚かまとめて送ったときは、全部届いてから1回で読みます。
- **グループトーク**: 「りまいんたろう、〜」と呼びかけるか、ボットをメンションしたときだけ答えます。グループに送った画像にはその場では反応せず、15分以内に「りまいんたろう、これ入れて」と呼びかけると、その画像を読んで登録します（家族写真などに勝手に反応しないため）。

画像や文章から登録するときは、同じ日の予定を確かめてから入れるので、同じお便りを2回送っても二重には登録されません。読み取れなかった日時は登録せずに聞き返します。

## LINE公式アカウント（Messaging API）の仕組み

**LINE公式アカウント** は、お店や企業が使っている「ビジネス用のLINEアカウント」です。ふつうの友だちと同じように友だち追加してトークできます。今回はこれを、夫婦専用のコンシェルジュの「顔」として使います。

**Messaging API** は、公式アカウントに届いたメッセージを自分のプログラムで受け取り、返事を送るための仕組みです。流れは次のとおりです。

```
 夫・妻のLINE  --①送信-->  LINEのサーバー  --②Webhook-->  このプログラム(Cloud Run)
                                                          ③本物のLINEからか確認
                                                          ④画像ならLINEから本体を取得
                                                          ⑤Claudeが考えてカレンダーを読み書き
 夫・妻のLINE  <--⑦表示--  LINEのサーバー  <--⑥返信API--  返信トークンを付けて送る
```

1. 夫婦がボットにテキストや画像を送ります。
2. LINEのサーバーが、あらかじめ登録したURL（**Webhook URL**。このプログラムの `/callback`）に、メッセージの内容をJSONで送ってきます。
3. 送られてきたデータには、**チャネルシークレット** を使った署名が付いています。プログラムはこれを確かめて、本当にLINEから来たものだけを受け付けます。
4. 画像のとき、Webhookに入っているのは「メッセージID」だけです。プログラムがそのIDを使って、LINEのサーバーから画像の本体をダウンロードします。
5. プログラムがClaudeに考えてもらい、Googleカレンダーを読み書きします。
6. Webhookに付いてくる **返信トークン** と **チャネルアクセストークン** を使って返信APIを呼びます。
7. トーク画面に返事が表示されます。

鍵は2つあります。

| 鍵 | 向き | 使い道 |
| --- | --- | --- |
| チャネルシークレット | LINE → プログラム | 届いたWebhookが本物か確かめる |
| チャネルアクセストークン | プログラム → LINE | 返信を送る、画像をダウンロードする |

メッセージの送り方も2種類あります。

- **返信**: 届いたメッセージへの返事です。返信トークンは1回だけ使え、受け取ってから時間がたつと使えなくなります。**無料で、月の通数にも数えられません。**
- **プッシュ**: プログラムから好きなときに送るメッセージです。月の無料通数（無料プランは200通）を使います。

このプログラムは返信だけで動きます。考えるのに時間がかかって返信トークンが使えなかったときだけ、プッシュで届けます。

グループにボットを招待すると、グループ内のメッセージもすべてWebhookで届きます。呼びかけられたときだけ答えるのは、プログラム側でそうしているためです。また、1対1のトークでは、考えている間に「…」のアニメーションを表示しています（グループではLINEの仕様で表示できません）。

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

#### 1-4. （任意）仕事のカレンダーから出張・移動を入れる

別のGoogleアカウント（会社のアカウントなど）のカレンダーを読んで、出張・移動の予定を共有カレンダーに写せます。アカウントを切り替える必要はなく、個人カレンダーと同じく、サービスアカウントに共有するだけです。

1. **仕事のアカウント** でパソコンのGoogleカレンダーを開き、自分のカレンダーの「設定と共有」→「特定のユーザーまたはグループと共有する」で、サービスアカウントのメールアドレス（`$SA`）を追加
2. 権限で **「予定の表示（すべての予定の詳細）」** を選ぶ
3. `.env`（LINEでも使うなら `deploy/env.yaml` も）の `MEMBERx_WORK_CALENDAR_ID` に、仕事のメールアドレスを入れる

> **手順2で「すべての予定の詳細」が選べないとき**: 会社の設定で、社外への共有が「時間枠のみ」に制限されています。この場合、空き時間探しには使えますが、どれが出張かは読み取れません。仕事のカレンダーの画面をスクショしてLINEで送れば、画像から登録できます。
>
> 仕事の予定のタイトルや場所は、出張かどうかを判断するためにClaude APIに送られます。会社のルール上問題ないかを確認してから設定してください。

使い方は、LINEかClaude Codeで「仕事の出張を入れて」「来月の出張を反映して」と頼むだけです（期間を言わなければ8週間先まで見ます）。

- 写すのは出張・前泊・移動（新幹線・飛行機など）だけで、ふつうの会議は写しません。
- 取引先名や会議の中身は書かず、「出張（大阪）」「移動（東京→新大阪）」のように写します。
- 同じ出張をもう一度写すと、新しく作らずに上書きします。日程が変わったら、もう一度頼めば直ります。
- 空き時間探しでも、仕事の予定で埋まっている時間を考慮します。
- 手順6を設定すると、3日に1回自動で取り込みます。

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

Claude Code には画像も貼り付けられるので、お便りの写真を貼って「この予定を入れて」と頼むこともできます。

夫婦それぞれがClaude Codeを使う場合は、各自のパソコンで同じ手順を行い、`MCP_USER_NAME` を自分の名前にします。

### 3. LINE公式アカウント（Messaging API）を作る

1. [LINE Official Account Manager](https://manager.line.biz/) で公式アカウントを作り（アカウント名は「りまいんたろう」。グループでメンションするときの名前にもなります）、「設定」→「Messaging API」で Messaging API を有効にする
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
printf '%s' "$(openssl rand -hex 24)"  | gcloud secrets create cron-secret --data-file=-   # 自動取り込み用の合言葉
for s in anthropic-api-key line-channel-secret line-channel-access-token cron-secret; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done

# 秘密情報以外の設定
cp deploy/env.example.yaml deploy/env.yaml   # カレンダーIDと二人の名前を記入

gcloud run deploy couple-concierge \
  --source . --region $REGION \
  --service-account $SA \
  --allow-unauthenticated \
  --max-instances 1 --timeout 600 \
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

これで完成です。LINEで「今週の予定は？」と送ったり、お便りの写真を送ったりしてみてください。

### 6. （任意）仕事のカレンダーを3日に1回自動で取り込む

手順1-4で仕事のカレンダーを設定していれば、Cloud Scheduler から3日に1回、出張・移動を自動で取り込めます。LINEには何も送らず、カレンダーに反映するだけです。

1回ごとに、次の2つを行います。

- **片付け**: 写した予定のうち、仕事のカレンダーで消された・取り消された・欠席にした・8週間より先へ延期されたものを削除する（プログラムで確実に行う）
- **取り込み**: 今日から8週間先までの仕事の予定を読み、出張・移動を写す。前に写したものは、日時や行き先が変わったときだけ上書きする（Claudeが判断。自動なので質問はせず、迷うものは写さない）

```bash
gcloud services enable cloudscheduler.googleapis.com
URL=$(gcloud run services describe couple-concierge --region $REGION --format 'value(status.url)')
gcloud scheduler jobs create http couple-work-trips \
  --location $REGION --schedule "0 6 */3 * *" --time-zone "Asia/Tokyo" \
  --uri "$URL/tasks/sync-work-trips" --http-method POST \
  --headers "X-Cron-Secret=$(gcloud secrets versions access latest --secret cron-secret)" \
  --attempt-deadline 600s

# すぐに1回動かして確かめる
gcloud scheduler jobs run couple-work-trips --location $REGION
```

- `0 6 */3 * *` は「毎月1日・4日・7日…28日（31日まである月は31日も）の朝6時」です。月をまたぐときだけ、間が1〜2日になることがあります。
- 結果は Cloud Run のログに「仕事のカレンダーの自動取り込み」として残ります。
- 手元で1回だけ試すときは `uv run couple-calendar-sync`（`.env` に `ANTHROPIC_API_KEY` が必要）。
- 見る期間は `WORK_SYNC_DAYS`（日数、既定56日＝8週間）で変えられます。

---

## LINEでの使い方のコツ

- 「私」「自分」は送った人のこと、と理解します。「ふたりで」「一緒に」は二人の予定になります。
- 画像を送ったあとに「これは、はなこの予定」「年は来年」などと続けて送ると、登録した内容を直してくれます。
- 文字の多いお便りは、紙全体がまっすぐ写るように撮ると読み取りが正確になります。
- 話の流れは30分覚えています（「それ19時に変えて」が通じます）。話題を変えたいときは「リセット」と送ります。
- 「ID」と送ると、自分のユーザーIDとトークのIDを返します。

## 料金の目安

- **LINE**: 無料プランで使えます。コンシェルジュの返事はすべて「返信」なので、月の通数に数えられません（返信が間に合わなかったときのプッシュだけが数に入ります）。
- **Cloud Run / Secret Manager / Cloud Scheduler**: 夫婦で使う程度なら、ほぼ無料枠に収まります（Cloud Scheduler は3ジョブまで無料）。
- **Claude API**: やりとり1回ごとに使った分だけかかります。画像は文字だけより多めにかかります。仕事のカレンダーの自動取り込みも、3日に1回その分がかかります。安くしたいときは `CLAUDE_EFFORT=low` にするか、`CLAUDE_MODEL` をより安いモデルに変えてください。

## ファイル構成

```
couple-calendar/
├── src/couple_calendar/
│   ├── config.py      設定（.env / 環境変数）
│   ├── gcal.py        Googleカレンダーの読み書き、担当者の判定、空き時間の計算
│   ├── tools.py       カレンダー操作ツール（Claude Code と LINE で共通）
│   ├── concierge.py   LINEのコンシェルジュ（Claude API + ツール）
│   ├── images.py      LINEで受け取った画像の縮小・向きの補正
│   ├── line_app.py    LINEのWebhookサーバー（FastAPI）
│   ├── mcp_server.py  Claude Code用のMCPサーバー
│   └── work_sync.py   仕事のカレンダーの自動取り込み（3日に1回）
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
| グループで返事がない | 「りまいんたろう、〜」で始めるか、ボットをメンションしているか（呼び方は `GROUP_TRIGGER_WORDS` で増やせます） |
| グループで画像を読んでくれない | 画像を送ってから15分以内に呼びかけているか（画像だけでは反応しません） |
| Claude Code で「まだ設定されていません」と出る | `couple-calendar/.env` を作り、`GOOGLE_CALENDAR_ID` などを入れたか |
| 個人カレンダーの空き時間が反映されない | 個人カレンダーをサービスアカウントに共有したか（返信に「参照できませんでした」と出ます） |
| 「仕事のカレンダーを読めません」 | 仕事のカレンダーをサービスアカウントに「すべての予定の詳細」で共有したか、`MEMBERx_WORK_CALENDAR_ID` が正しいか |
