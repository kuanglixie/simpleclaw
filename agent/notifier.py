from __future__ import annotations

import json
import re
from pathlib import Path

from telegram import Bot

from .models import Task


class Notifier:
    def __init__(self, token: str) -> None:
        self._bot = Bot(token=token) if token else None

    async def notify_task_result(self, task: Task, contract_json_path: Path) -> None:
        if not self._bot or not task.telegram_chat_id:
            return

        status = task.status.value
        summary = "(no summary available)"
        final_answer = ""
        errors: list[str] = []
        try:
            payload = json.loads(contract_json_path.read_text())
            status = payload.get("status", status)
            summary = payload.get("summary", summary)
            final_answer = str(payload.get("final_answer", "") or "")
            errors = payload.get("errors", []) or []
        except Exception:
            pass

        body = _build_user_facing_body(summary, final_answer)

        if status == "failed":
            parts = [f"[failed] {body}"]
            error_line = _extract_error_reason(errors)
            if error_line:
                parts.append(f"\nError: {error_line}")
            message = "\n".join(parts)
        else:
            message = body

        await self._safe_send(task.telegram_chat_id, message)

    async def send_text(self, chat_id: str, message: str) -> None:
        if not self._bot:
            return
        await self._safe_send(chat_id, message)

    async def _safe_send(self, chat_id: str, text: str) -> None:
        """Send with HTML parse mode, fall back to plain text on failure."""
        if not self._bot:
            return
        clamped = _clamp_message(text)
        html = _markdown_to_html(clamped)
        try:
            await self._bot.send_message(
                chat_id=chat_id, text=html, parse_mode="HTML",
            )
        except Exception:
            plain = _strip_all_markup(clamped)
            try:
                await self._bot.send_message(chat_id=chat_id, text=plain)
            except Exception:
                pass


_TELEGRAM_MSG_LIMIT = 4096


def _clamp_message(text: str, limit: int = _TELEGRAM_MSG_LIMIT - 100) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 4] + "\n..."


def _markdown_to_html(text: str) -> str:
    """Convert common markdown to Telegram-safe HTML.

    Telegram HTML supports: <b>, <i>, <code>, <pre>, <a>, <s>.
    Order matters: process fenced blocks first to avoid inner replacements.
    """
    result = text

    result = re.sub(
        r"```(\w*)\n(.*?)```",
        lambda m: f"<pre>{_escape_html(m.group(2).strip())}</pre>",
        result,
        flags=re.DOTALL,
    )
    result = re.sub(
        r"`([^`\n]+?)`",
        lambda m: f"<code>{_escape_html(m.group(1))}</code>",
        result,
    )

    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
    result = re.sub(r"\*(.+?)\*", r"<i>\1</i>", result)
    result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result)

    return result


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_all_markup(text: str) -> str:
    """Remove both markdown and HTML tags for plain-text fallback."""
    cleaned = text
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
    cleaned = re.sub(r"`(.+?)`", r"\1", cleaned)
    cleaned = re.sub(r"~~(.+?)~~", r"\1", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return cleaned


def _build_user_facing_body(summary: str, final_answer: str) -> str:
    """Pick the best content to show the user -- prefer final_answer over summary."""
    answer = _clean_text(final_answer)
    if answer and len(answer) > 60:
        return answer[:3800]
    summ = _clean_text(summary)
    if summ:
        return summ[:3800]
    return "(no summary available)"


def _clean_text(text: str) -> str:
    """Remove internal noise lines from model output."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if _is_internal_note_line(line):
            continue
        lines.append(raw)
    return "\n".join(lines).strip()


_INTERNAL_NOTE_MARKERS = (
    "prompts updated for server:",
    "resources updated for server:",
    "tools updated for server:",
    "i need to",
    "i'll ",
    "i will ",
    "i'm ",
    "looping.",
    "stuck in a loop",
)


def _is_internal_note_line(line: str) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _INTERNAL_NOTE_MARKERS)


_ERROR_PATTERN = re.compile(
    r"((?:Error|Exception|Timeout|Quota)\w*:\s*.+)", re.IGNORECASE
)


def _extract_error_reason(errors: list[str]) -> str:
    """Pull the most meaningful single-line reason from the errors list."""
    for err in errors:
        match = _ERROR_PATTERN.search(err)
        if match:
            reason = " ".join(match.group(1).split()).strip()
            return reason[:300]
    combined = " ".join(errors).strip()
    if combined:
        first_line = combined.split("\n")[0].strip()
        return first_line[:300]
    return ""

