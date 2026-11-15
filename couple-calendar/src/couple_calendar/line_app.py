"""LINE Messaging API の Webhook サーバー（Cloud Run などで動かす）。

エンドポイント:
  POST /callback           LINE からの Webhook（署名を検証する）
  POST /tasks/daily-brief  朝のお知らせを送る（Cloud Scheduler から X-Cron-Secret ヘッダー付きで呼ぶ）
  GET  /healthz            ヘルスチェック
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections import OrderedDict
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.webhooks import GroupSource, MessageEvent, RoomSource, TextMessageContent, UserSource

from .concierge import Concierge
from .config import Settings, get_settings
from .gcal import CoupleCalendar

log = logging.getLogger(__name__)

LINE_TEXT_LIMIT = 5000
LINE_MAX_MESSAGES = 5


def split_for_line(text: str, limit: int = LINE_TEXT_LIMIT - 100) -> list[str]:
    """LINE の1メッセージの文字数上限に合わせて分割する（最大5通）。"""
    chunks: list[str] = []
    rest = text.strip()
    while rest and len(chunks) < LINE_MAX_MESSAGES:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks or ["（空のメッセージ）"]


def _remove_span(text: str, index: int, length: int) -> str:
    """メンション部分（@名前）を取り除く。

    LINE の index/length は UTF-16 単位のことがあり、絵文字が前にあると Python の文字位置とずれる。
    両方で切ってみて、「@」で始まる方を採用する。
    """
    by_char = text[index : index + length]
    if by_char.startswith("@"):
        return text[:index] + text[index + length :]
    units = text.encode("utf-16-le")
    head, mention, tail = units[: index * 2], units[index * 2 : (index + length) * 2], units[(index + length) * 2 :]
    try:
        if mention.decode("utf-16-le").startswith("@"):
            return head.decode("utf-16-le") + tail.decode("utf-16-le")
    except UnicodeDecodeError:
        pass
    return text[:index] + text[index + length :]


class LineBot:
    def __init__(self, settings: Settings, concierge: Concierge) -> None:
        if not settings.line_channel_secret or not settings.line_channel_access_token:
            raise RuntimeError("LINE_CHANNEL_SECRET と LINE_CHANNEL_ACCESS_TOKEN を設定してください。")
        self.settings = settings
        self.concierge = concierge
        self.parser = WebhookParser(settings.line_channel_secret)
        self._line_config = Configuration(access_token=settings.line_channel_access_token)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()

    # ------------------------------------------------------------------ LINE API

    def _api(self) -> tuple[ApiClient, MessagingApi]:
        client = ApiClient(self._line_config)
        return client, MessagingApi(client)

    def reply(self, reply_token: str, text: str) -> None:
        client, api = self._api()
        with client:
            api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token, messages=[TextMessage(text=t) for t in split_for_line(text)]
                )
            )

    def push(self, to: str, text: str) -> None:
        client, api = self._api()
        with client:
            api.push_message(PushMessageRequest(to=to, messages=[TextMessage(text=t) for t in split_for_line(text)]))

    def show_loading(self, user_id: str) -> None:
        """「入力中…」のアニメーションを出す（1:1 トークのみ対応）。失敗しても処理は続ける。"""
        try:
            client, api = self._api()
            with client:
                api.show_loading_animation(ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=60))
        except Exception:
            log.warning("ローディング表示に失敗しました", exc_info=True)

    # ------------------------------------------------------------------ Webhook

    def _first_time(self, webhook_event_id: str | None) -> bool:
        """同じイベントの再送を一度だけ処理する。"""
        if not webhook_event_id:
            return True
        with self._seen_lock:
            if webhook_event_id in self._seen:
                return False
            self._seen[webhook_event_id] = None
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
            return True

    def _addressed_text(self, message: TextMessageContent) -> str | None:
        """グループでボット宛てのメッセージなら、呼びかけ部分を除いた本文を返す。宛てでなければ None。"""
        text = message.text
        mentionees = getattr(message.mention, "mentionees", None) or []
        self_mentions = [m for m in mentionees if getattr(m, "is_self", False)]
        if self_mentions:
            for m in sorted(self_mentions, key=lambda m: m.index, reverse=True):
                text = _remove_span(text, m.index, m.length)
            return text.strip()
        stripped = text.strip()
        for word in self.settings.group_trigger_words:
            if stripped.startswith(word):
                return stripped[len(word) :].lstrip(" 、,:：　").strip()
        return None

    def handle_events(self, events: list) -> None:
        for event in events:
            if not isinstance(event, MessageEvent) or not isinstance(event.message, TextMessageContent):
                continue
            if not self._first_time(event.webhook_event_id):
                continue
            try:
                self.handle_text(event)
            except Exception:
                log.exception("メッセージの処理に失敗しました")
                try:
                    self.reply(event.reply_token, "ごめんなさい、エラーが起きました。少し時間をおいてもう一度お試しください。")
                except Exception:
                    log.exception("エラー通知の送信にも失敗しました")

    def handle_text(self, event: MessageEvent) -> None:
        source = event.source
        user_id = getattr(source, "user_id", None)
        member = self.settings.member_by_line_id(user_id)
        text = event.message.text.strip()

        if isinstance(source, GroupSource):
            chat_id = source.group_id
        elif isinstance(source, RoomSource):
            chat_id = source.room_id
        else:
            chat_id = user_id

        # 「ID」と送ると、設定に使う ID を返す（メンバー、またはセットアップモード中のみ）
        if text.upper() in ("ID", "ＩＤ") and (member or self.settings.line_setup_mode):
            lines = [f"あなたのユーザーID: {user_id}"]
            if not isinstance(source, UserSource):
                lines.append(f"このトークのID: {chat_id}")
            self.reply(event.reply_token, "\n".join(lines))
            return

        if member is None:
            # 夫婦以外からのメッセージには応答しない（カレンダーを守るため）
            log.info("未登録のユーザーからのメッセージを無視しました: %s", user_id)
            if self.settings.line_setup_mode and isinstance(source, UserSource):
                self.reply(
                    event.reply_token,
                    f"セットアップ中です。あなたのユーザーIDは\n{user_id}\nです。"
                    "MEMBER1_LINE_USER_ID または MEMBER2_LINE_USER_ID に設定してください。",
                )
            return

        if isinstance(source, UserSource):
            self.show_loading(user_id)
        else:
            addressed = self._addressed_text(event.message)
            if addressed is None:
                return  # グループでは、呼びかけられたときだけ応答する
            text = addressed or "こんにちは"

        if text in ("リセット", "reset"):
            self.concierge.reset(chat_id)
            self.reply(event.reply_token, "会話をリセットしました。")
            return

        answer = self.concierge.reply(chat_id, member.name, text)
        self.reply(event.reply_token, answer)

    def send_daily_brief(self) -> int:
        if not self.settings.daily_brief_to:
            raise RuntimeError("DAILY_BRIEF_TO（送り先のユーザーIDかグループID）が設定されていません。")
        text = self.concierge.daily_brief()
        for to in self.settings.daily_brief_to:
            self.push(to, text)
        return len(self.settings.daily_brief_to)


def _default_bot() -> LineBot:
    settings = get_settings()
    calendar = CoupleCalendar(settings)
    return LineBot(settings, Concierge(settings, calendar))


def create_app(bot_factory: Callable[[], LineBot] = _default_bot) -> FastAPI:
    app = FastAPI(title="Couple Calendar Concierge")
    state: dict[str, LineBot] = {}
    lock = threading.Lock()

    def get_bot() -> LineBot:
        with lock:
            if "bot" not in state:
                state["bot"] = bot_factory()
            return state["bot"]

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.post("/callback")
    async def callback(request: Request) -> PlainTextResponse:
        signature = request.headers.get("x-line-signature", "")
        body = (await request.body()).decode("utf-8")
        bot = get_bot()
        try:
            events = bot.parser.parse(body, signature)
        except InvalidSignatureError:
            raise HTTPException(status_code=400, detail="invalid signature")
        # LINE の reply token は使い切りなので、応答を作り終えてから 200 を返す
        await run_in_threadpool(bot.handle_events, events)
        return PlainTextResponse("OK")

    @app.post("/tasks/daily-brief")
    async def daily_brief(request: Request) -> dict:
        bot = get_bot()
        secret = bot.settings.cron_secret
        given = request.headers.get("x-cron-secret", "")
        if not secret or not hmac.compare_digest(given, secret):
            raise HTTPException(status_code=403, detail="forbidden")
        sent = await run_in_threadpool(bot.send_daily_brief)
        return {"sent": sent}

    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
