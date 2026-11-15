from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json

import pytest
from fastapi.testclient import TestClient
from linebot.v3.messaging.exceptions import ApiException
from PIL import Image

from couple_calendar.line_app import GROUP_IMAGE_TTL, LineBot, _remove_span, create_app, split_for_line

from .conftest import make_settings


def _png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeConcierge:
    def __init__(self):
        self.calls: list[dict] = []
        self.resets: list[str] = []

    def reply(self, chat_id, speaker, text, images=()):
        self.calls.append({"chat": chat_id, "speaker": speaker, "text": text, "images": list(images)})
        return f"了解: {text}"

    def reset(self, chat_id):
        self.resets.append(chat_id)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RecordingBot(LineBot):
    def __init__(self, settings, concierge, clock):
        super().__init__(settings, concierge, clock=clock)
        self.replies: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, str]] = []
        self.loading: list[str] = []
        self.contents: dict[str, bytes] = {}
        self.fail_reply = False

    def reply(self, reply_token, text):
        if self.fail_reply:
            raise ApiException(status=400, reason="Invalid reply token")
        self.replies.append((reply_token, text))

    def push(self, to, text):
        self.pushes.append((to, text))

    def show_loading(self, user_id):
        self.loading.append(user_id)

    def download_content(self, message_id):
        return self.contents[message_id]


def _sign(body: str, secret: str = "line-secret") -> str:
    return base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _event(message: dict, source: dict, event_id: str) -> dict:
    return {
        "type": "message",
        "mode": "active",
        "timestamp": 1763251200000,
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False},
        "replyToken": f"rt-{event_id}",
        "source": source,
        "message": message,
    }


def text_event(text, source, *, event_id="ev-1", mention=None):
    message = {"type": "text", "id": f"m-{event_id}", "text": text, "quoteToken": "q"}
    if mention:
        message["mention"] = mention
    return _event(message, source, event_id)


def image_event(message_id, source, *, event_id=None, image_set=None, provider="line"):
    message = {"type": "image", "id": message_id, "quoteToken": "q", "contentProvider": {"type": provider}}
    if image_set:
        message["imageSet"] = image_set
    return _event(message, source, event_id or f"ev-{message_id}")


TARO = {"type": "user", "userId": "U_taro"}
HANA = {"type": "user", "userId": "U_hana"}
GROUP = {"type": "group", "groupId": "G1", "userId": "U_taro"}


@pytest.fixture
def setup():
    def _make(**overrides):
        concierge = FakeConcierge()
        clock = FakeClock()
        bot = RecordingBot(make_settings(**overrides), concierge, clock)
        client = TestClient(create_app(lambda: bot))
        return bot, concierge, client, clock

    return _make


def _post(client, events):
    body = json.dumps({"destination": "Ubot", "events": events}, ensure_ascii=False)
    return client.post("/callback", content=body.encode(), headers={"x-line-signature": _sign(body)})


# ---------------------------------------------------------------- テキスト


def test_rejects_bad_signature(setup):
    _, concierge, client, _ = setup()
    body = json.dumps({"destination": "Ubot", "events": [text_event("hi", TARO)]})
    res = client.post("/callback", content=body, headers={"x-line-signature": _sign(body, "wrong")})
    assert res.status_code == 400
    assert concierge.calls == []


def test_member_direct_message_goes_to_concierge(setup):
    bot, concierge, client, _ = setup()
    res = _post(client, [text_event("明日の予定は？", HANA)])
    assert res.status_code == 200
    assert concierge.calls == [{"chat": "U_hana", "speaker": "はなこ", "text": "明日の予定は？", "images": []}]
    assert bot.replies == [("rt-ev-1", "了解: 明日の予定は？")]
    assert bot.loading == ["U_hana"]


def test_stranger_is_ignored(setup):
    bot, concierge, client, _ = setup()
    stranger = {"type": "user", "userId": "U_stranger"}
    bot.contents["img"] = _png()
    _post(client, [text_event("予定見せて", stranger), image_event("img", stranger)])
    assert concierge.calls == [] and bot.replies == []


def test_setup_mode_tells_stranger_their_id(setup):
    bot, concierge, client, _ = setup(line_setup_mode=True)
    _post(client, [text_event("こんにちは", {"type": "user", "userId": "U_new"})])
    assert concierge.calls == []
    assert "U_new" in bot.replies[0][1]


def test_id_command_shows_group_id(setup):
    bot, _, client, _ = setup()
    _post(client, [text_event("ID", GROUP)])
    assert "G1" in bot.replies[0][1] and "U_taro" in bot.replies[0][1]


def test_group_needs_trigger_word_or_mention(setup):
    bot, concierge, client, _ = setup()
    _post(client, [text_event("今日の夕飯どうする？", GROUP, event_id="a")])
    assert concierge.calls == []

    _post(client, [text_event("コンシェルジュ、今週の予定は？", GROUP, event_id="b")])
    mention = {"mentionees": [{"index": 0, "length": 7, "type": "user", "userId": "Ubot", "isSelf": True}]}
    _post(client, [text_event("@カレンダー係 土曜空いてる？", GROUP, event_id="c", mention=mention)])
    assert [(c["chat"], c["speaker"], c["text"]) for c in concierge.calls] == [
        ("G1", "たろう", "今週の予定は？"),
        ("G1", "たろう", "土曜空いてる？"),
    ]
    assert bot.loading == []  # グループではローディング表示を使わない


def test_redelivered_event_is_handled_once(setup):
    _, concierge, client, _ = setup()
    ev = text_event("こんにちは", TARO, event_id="same")
    _post(client, [ev])
    _post(client, [ev])
    assert len(concierge.calls) == 1


def test_reset_command(setup):
    bot, concierge, client, _ = setup()
    _post(client, [text_event("リセット", TARO)])
    assert concierge.resets == ["U_taro"] and concierge.calls == []


def test_falls_back_to_push_when_reply_token_is_unusable(setup):
    bot, concierge, client, _ = setup()
    bot.fail_reply = True
    _post(client, [text_event("今週の予定は？", HANA)])
    assert bot.pushes == [("U_hana", "了解: 今週の予定は？")]


def test_no_daily_brief_endpoint(setup):
    _, _, client, _ = setup()
    assert client.post("/tasks/daily-brief").status_code == 404


# ---------------------------------------------------------------- 画像


def test_direct_image_is_read_right_away(setup):
    bot, concierge, client, _ = setup()
    bot.contents["img1"] = _png()
    _post(client, [image_event("img1", TARO)])
    assert len(concierge.calls) == 1
    call = concierge.calls[0]
    assert call["chat"] == "U_taro" and call["speaker"] == "たろう" and call["text"] is None
    assert [img.media_type for img in call["images"]] == ["image/png"]
    assert bot.replies[0][0] == "rt-ev-img1"
    assert bot.loading == ["U_taro"]


def test_image_set_is_read_once_when_complete(setup):
    bot, concierge, client, _ = setup()
    for i in (1, 2, 3):
        bot.contents[f"p{i}"] = _png((i * 60, 0, 0))
    # 届く順番が前後しても、番号順にまとめて1回で読む
    _post(client, [image_event("p2", TARO, image_set={"id": "set1", "index": 2, "total": 3})])
    _post(client, [image_event("p1", TARO, image_set={"id": "set1", "index": 1, "total": 3})])
    assert concierge.calls == [] and bot.replies == []
    _post(client, [image_event("p3", TARO, image_set={"id": "set1", "index": 3, "total": 3})])
    assert len(concierge.calls) == 1
    expected = [bot.contents[f"p{i}"] for i in (1, 2, 3)]
    assert [base64.b64decode(img.data) for img in concierge.calls[0]["images"]] == expected
    assert [token for token, _ in bot.replies] == ["rt-ev-p3"]


def test_image_set_without_index(setup):
    bot, concierge, client, _ = setup()
    bot.contents["a"], bot.contents["b"] = _png(), _png((0, 255, 0))
    _post(client, [image_event("a", TARO, image_set={"id": "s", "total": 2})])
    _post(client, [image_event("b", TARO, image_set={"id": "s", "total": 2})])
    assert len(concierge.calls) == 1 and len(concierge.calls[0]["images"]) == 2


def test_group_image_waits_for_a_call(setup):
    bot, concierge, client, _ = setup()
    bot.contents["flyer"] = _png()
    _post(client, [image_event("flyer", GROUP)])
    assert concierge.calls == [] and bot.replies == []  # グループの画像には反応しない

    _post(client, [text_event("コンシェルジュ、これ入れておいて", GROUP, event_id="t1")])
    call = concierge.calls[0]
    assert len(call["images"]) == 1
    assert call["text"].startswith("これ入れておいて")
    assert "画像を1枚添付" in call["text"]

    # 一度使った画像は次の呼びかけには付かない
    _post(client, [text_event("コンシェルジュ、明日は？", GROUP, event_id="t2")])
    assert concierge.calls[1]["images"] == [] and concierge.calls[1]["text"] == "明日は？"


def test_group_images_expire(setup):
    bot, concierge, client, clock = setup()
    bot.contents["old"] = _png()
    _post(client, [image_event("old", GROUP)])
    clock.now += GROUP_IMAGE_TTL + 1
    _post(client, [text_event("コンシェルジュ、これ入れて", GROUP, event_id="t")])
    assert concierge.calls[0]["images"] == []


def test_unreadable_image(setup):
    bot, concierge, client, _ = setup()
    bot.contents["bad"] = b"not an image"
    _post(client, [image_event("bad", TARO)])
    assert concierge.calls == []
    assert "読み込めませんでした" in bot.replies[0][1]


def test_external_image_is_not_downloaded(setup):
    bot, concierge, client, _ = setup()
    _post(client, [image_event("ext", TARO, provider="external")])
    assert concierge.calls == []
    assert "読み込めませんでした" in bot.replies[0][1]


# ---------------------------------------------------------------- 補助関数


def test_remove_mention_after_emoji():
    # 😀 は UTF-16 で2単位。LINE が UTF-16 で位置を数えても、文字で数えても取り除ける
    assert _remove_span("😀 @係 明日は？", 3, 2).split() == ["😀", "明日は？"]
    assert _remove_span("😀 @係 明日は？", 2, 2).split() == ["😀", "明日は？"]


def test_split_for_line():
    text = ("あ" * 3000 + "\n") * 3
    chunks = split_for_line(text)
    assert all(len(c) <= 4900 for c in chunks)
    assert "".join(chunks).replace("\n", "") == ("あ" * 9000)


def test_end_to_end_image_to_calendar(settings, calendar, service):
    """画像を送る → コンシェルジュ（Claude はモック）が予定を登録 → 返信、までを通しで確認する。"""
    from couple_calendar.concierge import Concierge

    from .test_concierge import FakeAnthropic, _message

    fake = FakeAnthropic([
        _message("msg_1", [{"type": "tool_use", "id": "t1", "name": "list_events",
                            "input": {"start_date": "2026-12-05", "end_date": "2026-12-05"}}], "tool_use"),
        _message("msg_2", [{"type": "tool_use", "id": "t2", "name": "create_event",
                            "input": {"title": "保護者会", "start": "2026-12-05T10:00", "end": "2026-12-05T11:30",
                                      "owner": "ふたり", "location": "体育館", "memo": "上履き持参"}}], "tool_use"),
        _message("msg_3", [{"type": "text", "text": "12/5(土) 10:00〜11:30 保護者会【ふたり】を登録しました。"}], "end_turn"),
    ])
    bot = RecordingBot(settings, Concierge(settings, calendar, client=fake.client()), FakeClock())
    bot.contents["notice"] = _png()
    _post(TestClient(create_app(lambda: bot)), [image_event("notice", HANA)])

    first_content = fake.requests[0]["messages"][-1]["content"]
    assert first_content[0]["type"] == "image" and first_content[1]["type"] == "text"
    assert "発言者: はなこ" in first_content[1]["text"]
    [event] = service.store.values()
    assert event["summary"] == "保護者会" and event["location"] == "体育館"
    assert bot.replies == [("rt-ev-notice", "12/5(土) 10:00〜11:30 保護者会【ふたり】を登録しました。")]
