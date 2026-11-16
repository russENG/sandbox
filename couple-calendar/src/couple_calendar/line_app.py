"""LINE Messaging API の Webhook サーバー（Cloud Run などで動かす）。

エンドポイント:
  POST /callback                LINE からの Webhook（署名を検証する）
  POST /tasks/sync-work-trips   仕事のカレンダーの出張・移動を取り込む（Cloud Scheduler から X-Cron-Secret 付きで呼ぶ）
  GET  /healthz                 ヘルスチェック

テキストと画像のメッセージを受け付ける。
  - 1:1 トーク: 送られたテキスト・画像にそのまま応答する。画像を複数枚まとめて送ったときは、全部そろってから1回で読む
  - グループ   : 呼びかけ（「りまいんたろう、〜」かメンション）にだけ応答する。グループに送られた画像は少しの間覚えておき、
                 次の呼びかけ（「りまいんたろう、これ入れて」など）と一緒に読む
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.messaging.exceptions import ApiException
from linebot.v3.webhooks import (
    GroupSource,
    ImageMessageContent,
    MessageEvent,
    RoomSource,
    TextMessageContent,
    UserSource,
)

from .concierge import IMAGE_ONLY_TEXT, Concierge
from .config import Member, Settings, get_settings
from .gcal import CoupleCalendar
from .images import ImageInput, prepare_image
from .work_sync import WorkTripSync, build_sync

log = logging.getLogger(__name__)

LINE_TEXT_LIMIT = 5000
LINE_MAX_MESSAGES = 5
MAX_IMAGES_PER_TURN = 10
IMAGE_SET_TTL = 10 * 60  # まとめ送りの画像がそろうのを待つ時間（秒）
GROUP_IMAGE_TTL = 15 * 60  # グループの画像を覚えておく時間（秒）
MAX_GROUP_IMAGES = 4
HONORIFICS = ("さん", "くん", "君", "ちゃん", "様")
SEPARATORS = " 、,，:：　!！"


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


@dataclass
class _ImageSet:
    created: float
    images: dict[int, ImageInput] = field(default_factory=dict)


@dataclass
class _Chat:
    member: Member | None
    user_id: str | None
    chat_id: str | None
    direct: bool  # 1:1 トークか


class LineBot:
    def __init__(self, settings: Settings, concierge: Concierge, clock: Callable[[], float] = time.monotonic) -> None:
        if not settings.line_channel_secret or not settings.line_channel_access_token:
            raise RuntimeError("LINE_CHANNEL_SECRET と LINE_CHANNEL_ACCESS_TOKEN を設定してください。")
        self.settings = settings
        self.concierge = concierge
        self.parser = WebhookParser(settings.line_channel_secret)
        self._line_config = Configuration(access_token=settings.line_channel_access_token)
        self._clock = clock
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._image_sets: dict[str, _ImageSet] = {}
        self._group_images: dict[str, list[tuple[float, ImageInput]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ LINE API

    def reply(self, reply_token: str, text: str) -> None:
        with ApiClient(self._line_config) as client:
            MessagingApi(client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token, messages=[TextMessage(text=t) for t in split_for_line(text)]
                )
            )

    def push(self, to: str, text: str) -> None:
        with ApiClient(self._line_config) as client:
            MessagingApi(client).push_message(
                PushMessageRequest(to=to, messages=[TextMessage(text=t) for t in split_for_line(text)])
            )

    def show_loading(self, user_id: str) -> None:
        """「入力中…」のアニメーションを出す（1:1 トークのみ対応）。失敗しても処理は続ける。"""
        try:
            with ApiClient(self._line_config) as client:
                MessagingApi(client).show_loading_animation(
                    ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=60)
                )
        except Exception:
            log.warning("ローディング表示に失敗しました", exc_info=True)

    def download_content(self, message_id: str) -> bytes:
        """送られた画像の中身を LINE から取得する（Webhook にはメッセージIDしか入っていない）。"""
        with ApiClient(self._line_config) as client:
            return bytes(MessagingApiBlob(client).get_message_content(message_id))

    def respond(self, reply_token: str, chat_id: str | None, text: str) -> None:
        """返信する。返信トークンが使えなかったとき（処理に時間がかかって期限が切れた等）だけプッシュで送る。

        返信は無料で通数にも数えられないが、プッシュは月の無料通数を使う。
        """
        try:
            self.reply(reply_token, text)
        except ApiException as e:
            if e.status != 400 or not chat_id:
                raise
            log.warning("返信トークンが使えなかったため、プッシュで送ります: %s", e.reason)
            self.push(chat_id, text)

    # ------------------------------------------------------------------ Webhook

    def _first_time(self, webhook_event_id: str | None) -> bool:
        """同じイベントの再送を一度だけ処理する。"""
        if not webhook_event_id:
            return True
        with self._lock:
            if webhook_event_id in self._seen:
                return False
            self._seen[webhook_event_id] = None
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
            return True

    def _chat(self, event: MessageEvent) -> _Chat:
        source = event.source
        user_id = getattr(source, "user_id", None)
        if isinstance(source, GroupSource):
            chat_id, direct = source.group_id, False
        elif isinstance(source, RoomSource):
            chat_id, direct = source.room_id, False
        else:
            chat_id, direct = user_id, True
        return _Chat(self.settings.member_by_line_id(user_id), user_id, chat_id, direct)

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
        # 「りまいん」と「りまいんたろう」の両方があるときに備え、長い呼び方から順に試す
        for word in sorted(self.settings.group_trigger_words, key=len, reverse=True):
            if stripped.startswith(word):
                rest = stripped[len(word) :]
                for honorific in HONORIFICS:  # 「りまいんたろうさん、〜」も呼びかけとして扱う
                    if rest.startswith(honorific):
                        rest = rest[len(honorific) :]
                        break
                return rest.lstrip(SEPARATORS).strip()
        return None

    def handle_events(self, events: list) -> None:
        for event in events:
            if not isinstance(event, MessageEvent):
                continue
            if not isinstance(event.message, (TextMessageContent, ImageMessageContent)):
                continue
            if not self._first_time(event.webhook_event_id):
                continue
            chat = self._chat(event)
            try:
                if isinstance(event.message, ImageMessageContent):
                    self.handle_image(event, chat)
                else:
                    self.handle_text(event, chat)
            except Exception:
                log.exception("メッセージの処理に失敗しました")
                try:
                    self.respond(
                        event.reply_token, chat.chat_id, "ごめんなさい、エラーが起きました。少し時間をおいてもう一度お試しください。"
                    )
                except Exception:
                    log.exception("エラー通知の送信にも失敗しました")

    # ---- テキスト

    def handle_text(self, event: MessageEvent, chat: _Chat) -> None:
        text = event.message.text.strip()

        # 「ID」と送ると、設定に使う ID を返す（メンバー、またはセットアップモード中のみ）
        if text.upper() in ("ID", "ＩＤ") and (chat.member or self.settings.line_setup_mode):
            lines = [f"あなたのユーザーID: {chat.user_id}"]
            if not chat.direct:
                lines.append(f"このトークのID: {chat.chat_id}")
            self.reply(event.reply_token, "\n".join(lines))
            return

        if chat.member is None:
            # 夫婦以外からのメッセージには応答しない（カレンダーを守るため）
            log.info("未登録のユーザーからのメッセージを無視しました: %s", chat.user_id)
            if self.settings.line_setup_mode and chat.direct:
                self.reply(
                    event.reply_token,
                    f"セットアップ中です。あなたのユーザーIDは\n{chat.user_id}\nです。"
                    "MEMBER1_LINE_USER_ID または MEMBER2_LINE_USER_ID に設定してください。",
                )
            return

        if not chat.direct:
            addressed = self._addressed_text(event.message)
            if addressed is None:
                return  # グループでは、呼びかけられたときだけ応答する
            text = addressed

        if text in ("リセット", "reset"):
            self.concierge.reset(chat.chat_id)
            self._take_group_images(chat.chat_id)
            self.reply(event.reply_token, "会話をリセットしました。")
            return

        images: list[ImageInput] = []
        if chat.direct:
            self.show_loading(chat.user_id)
        else:
            images = self._take_group_images(chat.chat_id)
            if images:
                text = (
                    f"{text or IMAGE_ONLY_TEXT}\n"
                    f"（このトークに少し前に送られた画像を{len(images)}枚添付します。発言の内容に関係するときだけ使ってください）"
                )

        answer = self.concierge.reply(chat.chat_id, chat.member.name, text, images)
        self.respond(event.reply_token, chat.chat_id, answer)

    # ---- 画像

    def handle_image(self, event: MessageEvent, chat: _Chat) -> None:
        if chat.member is None:
            log.info("未登録のユーザーからの画像を無視しました: %s", chat.user_id)
            return
        message: ImageMessageContent = event.message
        if chat.direct:
            self.show_loading(chat.user_id)

        provider = getattr(message.content_provider, "type", "line")
        if provider != "line":
            if chat.direct:
                self.respond(event.reply_token, chat.chat_id, "この画像は読み込めませんでした。保存してから送り直してみてください。")
            return
        try:
            image = prepare_image(self.download_content(message.id))
        except ValueError:
            if chat.direct:
                self.respond(event.reply_token, chat.chat_id, "この画像は読み込めませんでした。別の形式で送り直してみてください。")
            return

        if not chat.direct:
            self._remember_group_image(chat.chat_id, image)  # 次の呼びかけで使う
            return

        image_set = message.image_set
        if image_set is not None and (image_set.total or 0) > 1:
            images = self._collect_image_set(image_set.id, image_set.index, image_set.total, image)
            if images is None:
                return  # まだ全部そろっていない。最後の1枚が届いたときにまとめて返信する
        else:
            images = [image]

        text = None
        if len(images) > MAX_IMAGES_PER_TURN:
            images = images[:MAX_IMAGES_PER_TURN]
            text = f"{IMAGE_ONLY_TEXT}（画像が多いため、最初の{MAX_IMAGES_PER_TURN}枚だけ添付しています。残りは分けて送るよう伝えてください）"
        answer = self.concierge.reply(chat.chat_id, chat.member.name, text, images)
        self.respond(event.reply_token, chat.chat_id, answer)

    def _collect_image_set(
        self, set_id: str, index: int | None, total: int, image: ImageInput
    ) -> list[ImageInput] | None:
        """まとめ送りの画像をためる。全部そろったら番号順のリストを返し、まだなら None。"""
        now = self._clock()
        with self._lock:
            for key in [k for k, s in self._image_sets.items() if now - s.created > IMAGE_SET_TTL]:
                del self._image_sets[key]
            bucket = self._image_sets.setdefault(set_id, _ImageSet(created=now))
            slot = index if index and index not in bucket.images else max(bucket.images, default=0) + 1
            bucket.images[slot] = image
            if len(bucket.images) < total:
                return None
            del self._image_sets[set_id]
            return [bucket.images[k] for k in sorted(bucket.images)]

    def _remember_group_image(self, chat_id: str, image: ImageInput) -> None:
        now = self._clock()
        with self._lock:
            kept = [(t, img) for t, img in self._group_images.get(chat_id, []) if now - t <= GROUP_IMAGE_TTL]
            kept.append((now, image))
            self._group_images[chat_id] = kept[-MAX_GROUP_IMAGES:]

    def _take_group_images(self, chat_id: str) -> list[ImageInput]:
        now = self._clock()
        with self._lock:
            stored = self._group_images.pop(chat_id, [])
        return [img for t, img in stored if now - t <= GROUP_IMAGE_TTL]


def _default_bot() -> LineBot:
    settings = get_settings()
    calendar = CoupleCalendar(settings)
    return LineBot(settings, Concierge(settings, calendar))


def create_app(
    bot_factory: Callable[[], LineBot] = _default_bot,
    sync_factory: Callable[[], WorkTripSync] = build_sync,
) -> FastAPI:
    app = FastAPI(title="Couple Calendar Concierge")
    state: dict[str, object] = {}
    lock = threading.Lock()
    sync_lock = threading.Lock()

    def get_bot() -> LineBot:
        with lock:
            if "bot" not in state:
                state["bot"] = bot_factory()
            return state["bot"]  # type: ignore[return-value]

    def get_sync() -> WorkTripSync:
        with lock:
            if "sync" not in state:
                state["sync"] = sync_factory()
            return state["sync"]  # type: ignore[return-value]

    def run_sync(sync: WorkTripSync) -> dict:
        if not sync_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="already running")
        try:
            return sync.run().to_dict()
        finally:
            sync_lock.release()

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
        # LINE の返信トークンは使い切りなので、返信を送り終えてから 200 を返す
        await run_in_threadpool(bot.handle_events, events)
        return PlainTextResponse("OK")

    @app.post("/tasks/sync-work-trips")
    async def sync_work_trips(request: Request) -> dict:
        sync = get_sync()
        secret = sync.settings.cron_secret
        given = request.headers.get("x-cron-secret", "")
        if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
            raise HTTPException(status_code=403, detail="forbidden")
        return await run_in_threadpool(run_sync, sync)

    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
