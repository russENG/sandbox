from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from couple_calendar.mcp_server import build_instructions, create_server


def test_tools_are_exposed_and_callable(settings, calendar, service):
    server = create_server(settings, calendar)

    async def run():
        names = {t.name for t in await server.list_tools()}
        assert names == {
            "get_calendar_info", "list_events", "create_event", "update_event", "delete_event", "find_free_time",
        }
        schema = next(t for t in await server.list_tools() if t.name == "create_event").input_schema
        assert schema["required"] == ["title", "start"]
        result = await server.call_tool("create_event", {"title": "テスト", "start": "2026-11-20T10:00", "owner": "たろう"})
        payload = json.loads(result.content[0].text)
        assert payload["created"]["owner"] == "たろう"

    asyncio.run(run())
    assert len(service.store) == 1


def test_calendar_errors_reach_the_client_readably(settings, calendar):
    server = create_server(settings, calendar)
    with pytest.raises(ToolError, match="担当者「じろう」が分かりません"):
        asyncio.run(server.call_tool("create_event", {"title": "x", "start": "2026-11-20T10:00", "owner": "じろう"}))


def test_instructions_mention_the_user(settings):
    text = build_instructions(settings)
    assert "たろうさんとはなこさん" in text
    assert "利用者はたろうさん" in text
