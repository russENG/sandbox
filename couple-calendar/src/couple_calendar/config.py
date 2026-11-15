"""環境変数から設定を読み込む。

ローカルでは couple-calendar/.env を読み込み、Cloud Run では環境変数・Secret Manager から渡す。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[2]

BOTH = "both"
# 「ふたり」の予定として受け付ける言い方
BOTH_ALIASES = {"both", "ふたり", "二人", "2人", "夫婦", "ふたりとも", "一緒", "共通"}


@dataclass(frozen=True)
class Member:
    key: str  # "member1" / "member2"（カレンダーに保存する識別子）
    name: str  # 呼び名。予定の担当者として表示する
    color_id: str  # Googleカレンダーの予定の色 (1〜11)
    line_user_id: str | None = None
    email: str | None = None  # Googleアカウント。カレンダーアプリで直接入れた予定の担当者判定に使う
    personal_calendar_id: str | None = None  # 個人カレンダー（空き時間検索で参照。読み取りのみ）
    work_calendar_id: str | None = None  # 仕事のカレンダー（出張・移動の取り込みと空き時間検索で参照。読み取りのみ）


@dataclass(frozen=True)
class Settings:
    calendar_id: str
    timezone: str
    members: tuple[Member, Member]
    both_label: str
    both_color_id: str
    claude_model: str
    claude_effort: str
    line_channel_secret: str | None
    line_channel_access_token: str | None
    line_setup_mode: bool
    group_trigger_words: tuple[str, ...]
    mcp_user_name: str | None

    def member_by_key(self, key: str) -> Member | None:
        return next((m for m in self.members if m.key == key), None)

    def member_by_line_id(self, line_user_id: str | None) -> Member | None:
        if not line_user_id:
            return None
        return next((m for m in self.members if m.line_user_id == line_user_id), None)

    def owner_label(self, owner_key: str | None) -> str:
        if owner_key == BOTH:
            return self.both_label
        member = self.member_by_key(owner_key) if owner_key else None
        return member.name if member else "未設定"

    def resolve_owner(self, text: str) -> str:
        """「たろう」「ふたり」などの入力を、保存用のキー (member1 / member2 / both) に変換する。"""
        value = text.strip()
        if value in BOTH_ALIASES or value == self.both_label:
            return BOTH
        for m in self.members:
            if value in (m.name, m.key):
                return m.key
        choices = "、".join([m.name for m in self.members] + [self.both_label])
        raise ValueError(f"担当者「{text}」が分かりません。{choices} のいずれかを指定してください。")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name, "") or ""
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def _member(index: int, default_name: str, default_color: str) -> Member:
    p = f"MEMBER{index}_"
    return Member(
        key=f"member{index}",
        name=_env(p + "NAME", default_name),
        color_id=_env(p + "COLOR", default_color),
        line_user_id=_env(p + "LINE_USER_ID"),
        email=_env(p + "EMAIL"),
        personal_calendar_id=_env(p + "PERSONAL_CALENDAR_ID"),
        work_calendar_id=_env(p + "WORK_CALENDAR_ID"),
    )


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env", override=False)
    key_file = _env("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file:  # ~/... と書かれていても読めるようにする
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.expanduser(key_file)
    calendar_id = _env("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID が設定されていません。共有カレンダーのカレンダーIDを .env か環境変数に設定してください。"
        )
    return Settings(
        calendar_id=calendar_id,
        timezone=_env("TIMEZONE", "Asia/Tokyo"),
        # 色の既定値: 9=ブルーベリー, 4=フラミンゴ, 10=バジル
        members=(_member(1, "夫", "9"), _member(2, "妻", "4")),
        both_label=_env("BOTH_LABEL", "ふたり"),
        both_color_id=_env("BOTH_COLOR", "10"),
        claude_model=_env("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=_env("CLAUDE_EFFORT", "medium"),
        line_channel_secret=_env("LINE_CHANNEL_SECRET"),
        line_channel_access_token=_env("LINE_CHANNEL_ACCESS_TOKEN"),
        line_setup_mode=(_env("LINE_SETUP_MODE", "false") or "").lower() in ("1", "true", "yes"),
        group_trigger_words=_env_list("GROUP_TRIGGER_WORDS") or ("コンシェルジュ",),
        mcp_user_name=_env("MCP_USER_NAME"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
