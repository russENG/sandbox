from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from couple_calendar.line_app import LineBot, create_app, split_for_line

from .conftest import make_settings


class FakeConcierge:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.resets: list[str] = []

    def reply(self, chat_id, speaker, text):
        self.calls.append((chat_id, speaker, text))
        return f"了解: {text}"

    def reset(self, chat_id):
        self.resets.append(chat_id)

    def daily_brief(self):
        return "おはようございます。今日の予定はありません。"


class RecordingBot(LineBot):
    def __init__(self, settings, concierge):
        super().__init__(settings, concierge)
        self.replies: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, str]] = []
        self.loading: list[str] = []

    def reply(self, reply_token, text):
        self.replies.append((reply_token, text))

    def push(self, to, text):
        self.pushes.append((to, text))

    def show_loading(self, user_id):
        self.loading.append(user_id)


def _sign(body: str, secret: str = "line-secret") -> str:
    return base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _event(text: str, source: dict, *, event_id: str = "ev-1", mention: dict | None = None) -> dict:
    message = {"type": "text", "id": "m1", "text": text, "quoteToken": "q"}
    if mention:
        message["mention"] = mention
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


@pytest.fixture
def setup():
    def _make(**overrides):
        concierge = FakeConcierge()
        bot = RecordingBot(make_settings(**overrides), concierge)
        client = TestClient(create_app(lambda: bot))
        return bot, concierge, client

    return _make


def _post(client, events):
    body = json.dumps({"destination": "Ubot", "events": events}, ensure_ascii=False)
    return client.post("/callback", content=body.encode(), headers={"x-line-signature": _sign(body)})


def test_rejects_bad_signature(setup):
    _, concierge, client = setup()
    body = json.dumps({"destination": "Ubot", "events": [_event("hi", {"type": "user", "userId": "U_taro"})]})
    res = client.post("/callback", content=body, headers={"x-line-signature": _sign(body, "wrong")})
    assert res.status_code == 400
    assert concierge.calls == []


def test_member_direct_message_goes_to_concierge(setup):
    bot, concierge, client = setup()
    res = _post(client, [_event("明日の予定は？", {"type": "user", "userId": "U_hana"})])
    assert res.status_code == 200
    assert concierge.calls == [("U_hana", "はなこ", "明日の予定は？")]
    assert bot.replies == [("rt-ev-1", "了解: 明日の予定は？")]
    assert bot.loading == ["U_hana"]


def test_stranger_is_ignored(setup):
    bot, concierge, client = setup()
    _post(client, [_event("予定見せて", {"type": "user", "userId": "U_stranger"})])
    assert concierge.calls == [] and bot.replies == []


def test_setup_mode_tells_stranger_their_id(setup):
    bot, concierge, client = setup(line_setup_mode=True)
    _post(client, [_event("こんにちは", {"type": "user", "userId": "U_new"})])
    assert concierge.calls == []
    assert "U_new" in bot.replies[0][1]


def test_id_command_shows_group_id(setup):
    bot, _, client = setup()
    _post(client, [_event("ID", {"type": "group", "groupId": "G1", "userId": "U_taro"})])
    assert "G1" in bot.replies[0][1] and "U_taro" in bot.replies[0][1]


def test_group_needs_trigger_word_or_mention(setup):
    bot, concierge, client = setup()
    group = {"type": "group", "groupId": "G1", "userId": "U_taro"}
    _post(client, [_event("今日の夕飯どうする？", group, event_id="a")])
    assert concierge.calls == []

    _post(client, [_event("コンシェルジュ、今週の予定は？", group, event_id="b")])
    mention = {"mentionees": [{"index": 0, "length": 7, "type": "user", "userId": "Ubot", "isSelf": True}]}
    _post(client, [_event("@カレンダー係 土曜空いてる？", group, event_id="c", mention=mention)])
    assert concierge.calls == [("G1", "たろう", "今週の予定は？"), ("G1", "たろう", "土曜空いてる？")]
    assert bot.loading == []  # グループではローディング表示を使わない


def test_redelivered_event_is_handled_once(setup):
    _, concierge, client = setup()
    ev = _event("こんにちは", {"type": "user", "userId": "U_taro"}, event_id="same")
    _post(client, [ev])
    _post(client, [ev])
    assert len(concierge.calls) == 1


def test_reset_command(setup):
    bot, concierge, client = setup()
    _post(client, [_event("リセット", {"type": "user", "userId": "U_taro"})])
    assert concierge.resets == ["U_taro"] and concierge.calls == []


def test_daily_brief_requires_secret(setup):
    bot, _, client = setup()
    assert client.post("/tasks/daily-brief").status_code == 403
    assert client.post("/tasks/daily-brief", headers={"x-cron-secret": "nope"}).status_code == 403
    res = client.post("/tasks/daily-brief", headers={"x-cron-secret": "cron-secret"})
    assert res.json() == {"sent": 1}
    assert bot.pushes == [("G_group", "おはようございます。今日の予定はありません。")]


def test_remove_mention_after_emoji():
    from couple_calendar.line_app import _remove_span

    # 😀 は UTF-16 で2単位。LINE が UTF-16 で位置を数えても、文字で数えても取り除ける
    assert _remove_span("😀 @係 明日は？", 3, 2).strip() == "😀  明日は？".strip()
    assert _remove_span("😀 @係 明日は？", 2, 2).strip() == "😀  明日は？".strip()


def test_split_for_line():
    text = ("あ" * 3000 + "\n") * 3
    chunks = split_for_line(text)
    assert all(len(c) <= 4900 for c in chunks)
    assert "".join(chunks).replace("\n", "") == ("あ" * 9000)
