"""LINE で会話するスケジュール・コンシェルジュ（Claude API + カレンダーツール）。

チャット（1:1 トーク / グループ）ごとに会話の流れを覚える。最後の発言から一定時間たつか、
会話が長くなりすぎたら、新しい会話としてやり直す（履歴は途中を削らず、丸ごと作り直す）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anthropic
from anthropic import beta_tool

from .config import Settings
from .gcal import CoupleCalendar, weekday_ja
from .tools import build_tools

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ITERATIONS = 10

SYSTEM_TEMPLATE = """\
あなたは{m1}さんと{m2}さんの夫婦専属のスケジュール・コンシェルジュです。LINEで二人と会話しながら、夫婦の共有Googleカレンダーを管理します。

# カレンダーのルール
- 予定には担当者があります:「{m1}」「{m2}」「{both}」（二人の予定）。登録時は必ず担当者を決めてください。
- ユーザーのメッセージ冒頭に [現在日時: … / 発言者: …] が付きます。「私」「自分」は発言者のこと、「相手」「パートナー」はもう一人のことです。
- 「明日」「来週の金曜」「月末」などは、現在日時（タイムゾーン {tz}）から計算し、曜日も確かめてから使ってください。
- 担当者が読み取れないときは、二人に関係しそうなら「{both}」、そうでなければ発言者の予定にして、どちらにしたかを返信で伝えてください。
- 時刻が分からない予定は、終日予定として登録するか時刻を聞き返すかを判断し、どちらにしたかを伝えてください。
- 予定を変更・削除するときは、まず list_events で対象を探してください。当てはまる予定が複数あるときは、どれのことか聞いてから操作します。
- 削除は対象がはっきりしているときだけ行ってください。
- 「空いてる日」「いつなら会える」などの相談には find_free_time を使ってください。
- ツールがエラーを返したら、内容を分かりやすく伝え、必要なら言い直しをお願いしてください。

# 返信のスタイル
- LINEのトークで読まれるので、短く、やわらかい丁寧語で書いてください。
- Markdown（#、**、表、コードブロック）は使わず、必要なら「・」の箇条書きと改行だけを使ってください。
- 予定は「11/20(金) 19:00〜21:00 ディナー【{both}】」の形で書いてください。終日予定は「11/23(月) 終日 結婚記念日【{both}】」。
- 登録・変更・削除をしたら、日付（曜日）・時刻・担当者を一行で復唱してください。
- 予定の id などの内部情報は見せないでください。
- カレンダー以外の雑談には、ひとことで感じよく応じてください。
"""

DAILY_BRIEF_PROMPT = (
    "朝のお知らせを作ってください。今日と明日の予定を list_events で確認し、"
    "あいさつに続けて二人の予定を短くまとめてください。予定がなければ、その旨とひとことを添えてください。"
)

ERROR_REPLY = "ごめんなさい、うまく処理できませんでした。少し時間をおいて、もう一度送ってみてください。"
REFUSAL_REPLY = "ごめんなさい、その内容にはお答えできません。予定のことなら何でも聞いてください。"


def build_system_prompt(settings: Settings) -> str:
    m1, m2 = settings.members
    return SYSTEM_TEMPLATE.format(m1=m1.name, m2=m2.name, both=settings.both_label, tz=settings.timezone)


@dataclass
class _Session:
    thread: Any
    last_used: datetime
    lock: threading.Lock = field(default_factory=threading.Lock)


class Concierge:
    def __init__(
        self,
        settings: Settings,
        calendar: CoupleCalendar,
        client: anthropic.Anthropic | None = None,
        *,
        session_ttl: timedelta = timedelta(minutes=30),
        max_messages: int = 80,
    ) -> None:
        self.settings = settings
        self.calendar = calendar
        self.client = client or anthropic.Anthropic()
        self.tools = [beta_tool(fn) for fn in build_tools(calendar)]
        self.system = build_system_prompt(settings)
        self.session_ttl = session_ttl
        self.max_messages = max_messages
        self._sessions: dict[str, _Session] = {}
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------------ 会話

    def _new_thread(self) -> Any:
        # system / tools / 各種設定は会話中ずっと同じ値を使う（キャッシュと思考ブロックの再利用のため）。
        # 現在日時など毎回変わる情報は、ユーザーメッセージの先頭に入れる。
        return self.client.beta.messages.thread(
            model=self.settings.claude_model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            output_config={"effort": self.settings.claude_effort},
            cache_control={"type": "ephemeral"},
            # 安全性の判定で断られたときは、サーバー側で別モデルに自動で引き継ぐ
            betas=[FALLBACK_BETA],
            fallbacks="default",
            unsupported_request_behavior="continue_unthreaded",
        )

    def _session(self, chat_id: str, now: datetime) -> _Session:
        with self._sessions_lock:
            session = self._sessions.get(chat_id)
            stale = session is not None and (
                now - session.last_used > self.session_ttl or len(session.thread.messages) > self.max_messages
            )
            if session is None or stale:
                session = _Session(thread=self._new_thread(), last_used=now)
                self._sessions[chat_id] = session
            return session

    def reset(self, chat_id: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(chat_id, None)

    def _header(self, now: datetime, speaker: str | None) -> str:
        return f"[現在日時: {now:%Y-%m-%d}({weekday_ja(now.date())}) {now:%H:%M} / 発言者: {speaker or '不明'}]"

    def _run(self, thread: Any, text: str) -> tuple[str, bool]:
        """1ターン実行して (返信テキスト, この会話を続けてよいか) を返す。"""
        thread.append({"role": "user", "content": text})
        final = thread.tool_runner(max_iterations=MAX_TOOL_ITERATIONS).until_done()
        if final.stop_reason == "refusal":
            return REFUSAL_REPLY, False
        reply = "\n".join(b.text for b in final.content if b.type == "text").strip()
        # ツール呼び出しの途中で終わった会話は続けられないので、作り直す
        healthy = final.stop_reason in ("end_turn", "stop_sequence")
        return reply or ERROR_REPLY, healthy

    def reply(self, chat_id: str, speaker: str | None, text: str) -> str:
        now = self.calendar.now()
        session = self._session(chat_id, now)
        with session.lock:
            try:
                answer, healthy = self._run(session.thread, f"{self._header(now, speaker)}\n{text}")
            except anthropic.RateLimitError:
                log.warning("Claude API のレート制限に当たりました")
                self.reset(chat_id)
                return "いま混み合っています。1分ほどおいて、もう一度送ってみてください。"
            except Exception:
                # 途中で失敗した会話は続きから再開できないので、作り直す
                log.exception("コンシェルジュの応答に失敗しました")
                self.reset(chat_id)
                return ERROR_REPLY
            session.last_used = now
        if not healthy:
            self.reset(chat_id)
        return answer

    def daily_brief(self) -> str:
        """朝のお知らせ文を作る（チャットの会話履歴とは別の、使い捨ての会話）。"""
        now = self.calendar.now()
        answer, _ = self._run(self._new_thread(), f"{self._header(now, 'システム（定時のお知らせ）')}\n{DAILY_BRIEF_PROMPT}")
        return answer
