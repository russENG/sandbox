from __future__ import annotations

import json
from datetime import timedelta

import anthropic
import httpx2
import pytest

from couple_calendar.concierge import ERROR_REPLY, REFUSAL_REPLY, Concierge


def _message(msg_id: str, content: list[dict], stop_reason: str) -> dict:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class FakeAnthropic:
    """決められた順に応答を返す Messages API のモック。送られたリクエストを記録する。"""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[httpx2.Headers] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        return httpx2.Response(200, json=self.responses.pop(0))

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key="test-key",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )


def _tool_results(request: dict) -> list[dict]:
    return [
        block
        for msg in request["messages"]
        if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"]
        if block.get("type") == "tool_result"
    ]


def test_tool_loop_creates_event_and_replies(settings, calendar, service):
    fake = FakeAnthropic(
        [
            _message(
                "msg_1",
                [{
                    "type": "tool_use", "id": "toolu_1", "name": "create_event",
                    "input": {"title": "ディナー", "start": "2026-11-20T19:00", "end": "2026-11-20T21:00", "owner": "ふたり"},
                }],
                "tool_use",
            ),
            _message("msg_2", [{"type": "text", "text": "11/20(金) 19:00〜21:00 ディナー【ふたり】で登録しました。"}], "end_turn"),
        ]
    )
    concierge = Concierge(settings, calendar, client=fake.client())
    answer = concierge.reply("U_taro", "たろう", "金曜の夜7時から9時、ふたりでディナー入れて")

    assert "登録しました" in answer
    assert [e["summary"] for e in service.store.values()] == ["ディナー"]

    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in fake.headers[0]["anthropic-beta"]
    assert {t["name"] for t in first["tools"]} >= {"list_events", "create_event", "find_free_time"}
    user_text = first["messages"][-1]["content"]
    assert user_text.startswith("[現在日時: 2026-11-16(月) 08:00 / 発言者: たろう]")
    assert "たろうさんとはなこさん" in json.dumps(first["system"], ensure_ascii=False)

    results = _tool_results(fake.requests[1])
    assert results and not results[0].get("is_error")
    assert "ディナー" in json.dumps(results[0], ensure_ascii=False)


def test_tool_error_is_reported_to_model(settings, calendar):
    fake = FakeAnthropic(
        [
            _message(
                "msg_1",
                [{"type": "tool_use", "id": "toolu_1", "name": "create_event",
                  "input": {"title": "x", "start": "2026-11-20T10:00", "owner": "じろう"}}],
                "tool_use",
            ),
            _message("msg_2", [{"type": "text", "text": "担当者を教えてください。"}], "end_turn"),
        ]
    )
    concierge = Concierge(settings, calendar, client=fake.client())
    concierge.reply("U_taro", "たろう", "じろうの予定入れて")
    results = _tool_results(fake.requests[1])
    assert results[0]["is_error"] is True


def test_conversation_continues_then_expires(settings, calendar):
    fake = FakeAnthropic(
        [
            _message("msg_1", [{"type": "text", "text": "はい"}], "end_turn"),
            _message("msg_2", [{"type": "text", "text": "覚えています"}], "end_turn"),
            _message("msg_3", [{"type": "text", "text": "はじめまして"}], "end_turn"),
        ]
    )
    concierge = Concierge(settings, calendar, client=fake.client(), session_ttl=timedelta(minutes=30))
    concierge.reply("chat", "たろう", "こんにちは")
    concierge.reply("chat", "たろう", "さっきの件")
    # 2回目は同じ会話の続き（前の発言を含む会話として送られる）
    second = fake.requests[1]
    sent_text = json.dumps(second, ensure_ascii=False)
    assert "さっきの件" in sent_text
    assert ("こんにちは" in sent_text) or (second.get("thread", {}).get("type") == "continue")

    # 30分以上たつと新しい会話になる
    calendar.fixed_now = calendar.fixed_now + timedelta(minutes=31)
    concierge.reply("chat", "たろう", "おはよう")
    third = fake.requests[2]
    assert "こんにちは" not in json.dumps(third, ensure_ascii=False)
    assert third.get("thread", {}).get("type") != "continue"


def test_refusal_resets_session(settings, calendar):
    fake = FakeAnthropic([_message("msg_1", [], "refusal")])
    concierge = Concierge(settings, calendar, client=fake.client())
    assert concierge.reply("chat", "たろう", "...") == REFUSAL_REPLY
    assert "chat" not in concierge._sessions


def test_api_failure_returns_friendly_message(settings, calendar):
    def boom(request):
        return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "x"}})

    client = anthropic.Anthropic(
        api_key="k", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(boom))
    )
    concierge = Concierge(settings, calendar, client=client)
    assert concierge.reply("chat", "たろう", "こんにちは") == ERROR_REPLY
    assert "chat" not in concierge._sessions


@pytest.mark.parametrize("stop_reason", ["max_tokens", "tool_use"])
def test_unfinished_turn_resets_session(settings, calendar, stop_reason):
    fake = FakeAnthropic([_message("msg_1", [{"type": "text", "text": "途中"}], stop_reason)] * 12)
    concierge = Concierge(settings, calendar, client=fake.client())
    concierge.reply("chat", "たろう", "こんにちは")
    assert "chat" not in concierge._sessions


def test_images_are_sent_before_the_text(settings, calendar):
    from couple_calendar.concierge import IMAGE_ONLY_TEXT
    from couple_calendar.images import ImageInput

    fake = FakeAnthropic([_message("msg_1", [{"type": "text", "text": "読み取りました"}], "end_turn")])
    concierge = Concierge(settings, calendar, client=fake.client())
    image = ImageInput("image/png", "aGVsbG8=")
    concierge.reply("U_taro", "たろう", None, [image])
    content = fake.requests[0]["messages"][-1]["content"]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    assert content[1]["type"] == "text"
    assert content[1]["text"].endswith(IMAGE_ONLY_TEXT)
    assert "発言者: たろう" in content[1]["text"]


def test_system_prompt_covers_images_and_duplicates(settings, calendar):
    concierge = Concierge(settings, calendar, client=FakeAnthropic([]).client())
    assert "画像" in concierge.system and "二重に登録しない" in concierge.system


def test_system_prompt_covers_work_calendar(settings, calendar):
    concierge = Concierge(settings, calendar, client=FakeAnthropic([]).client())
    assert "list_work_events" in concierge.system
    assert any(t.name == "list_work_events" for t in concierge.tools)


def test_concierge_introduces_itself_by_name(settings, calendar):
    concierge = Concierge(settings, calendar, client=FakeAnthropic([]).client())
    assert concierge.system.startswith("あなたは「りまいんたろう」という名前の")
