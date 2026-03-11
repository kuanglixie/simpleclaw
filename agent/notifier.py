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
        parts = [f"[{status}] {body}"]
        if status == "failed" and errors:
            error_line = _extract_error_reason(errors)
            if error_line:
                parts.append(f"\nError: {error_line}")
        message = "\n".join(parts)
        await self._safe_send(task.telegram_chat_id, message)

    async def send_text(self, chat_id: str, message: str) -> None:
        if not self._bot:
            return
        await self._safe_send(chat_id, message)

    async def _safe_send(self, chat_id: str, text: str) -> None:
        """Send a message, falling back to plain text if Markdown parsing fails."""
        if not self._bot:
            return
        clamped = _clamp_message(text)
        try:
            await self._bot.send_message(
                chat_id=chat_id, text=clamped, parse_mode="Markdown",
            )
        except Exception:
            clean = _strip_markdown(clamped)
            await self._bot.send_message(chat_id=chat_id, text=clean)


_TELEGRAM_MSG_LIMIT = 4096


def _clamp_message(text: str, limit: int = _TELEGRAM_MSG_LIMIT - 50) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 4] + "\n..."


def _strip_markdown(text: str) -> str:
    """Remove Markdown formatting that Telegram might choke on."""
    cleaned = text
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)
    cleaned = re.sub(r"`(.+?)`", r"\1", cleaned)
    cleaned = re.sub(r"~~(.+?)~~", r"\1", cleaned)
    return cleaned


def _build_user_facing_body(summary: str, final_answer: str) -> str:
    """Pick the best content to show the user — prefer final_answer over summary."""
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

