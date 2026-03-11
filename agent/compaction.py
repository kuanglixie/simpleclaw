"""Context compaction: summarize older conversation history.

Inspired by OpenClaw's compaction mechanism. When session history grows
large, older messages are summarized into a compact entry while recent
messages are kept intact. This prevents context window overflow.

Includes a pre-compaction memory flush: before summarizing, the model
extracts durable facts and writes them to MEMORY.md so important
knowledge survives across compaction cycles.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.genai import types


COMPACTION_MARKER = "[COMPACTED_SUMMARY]"

COMPACTION_SYSTEM_PROMPT = """\
You are a conversation compaction assistant. Summarize the following older \
conversation turns into a concise but complete summary. Preserve:

1. Key decisions and conclusions
2. Important facts, file paths, URLs, and identifiers
3. Task progress and current state
4. Any outstanding action items or blockers

Be concise but don't lose critical details. Output only the summary, \
no preamble."""

MEMORY_EXTRACTION_PROMPT = """\
You are a memory extraction assistant. Review the conversation below and \
extract any facts that should be permanently remembered. Focus on:

1. User preferences or working patterns discovered
2. Important project names, file paths, or identifiers
3. People mentioned and their roles or relationships
4. Decisions made that affect future behavior
5. Recurring patterns ("user always does X when Y")

Output ONLY a bullet list of facts to remember, one per line starting \
with "- ". If nothing is worth remembering permanently, output exactly \
"NOTHING_NEW". Do not include transient task details or status updates."""


@dataclass
class CompactionConfig:
    enabled: bool = True
    max_session_messages: int = 40
    keep_recent: int = 12
    max_context_chars: int = 80_000
    model: str = ""  # empty = use primary model


@dataclass
class CompactionResult:
    did_compact: bool
    summary: str = ""
    messages_compacted: int = 0
    messages_kept: int = 0


def needs_compaction(
    messages: list[tuple[str, str]],
    config: CompactionConfig,
) -> bool:
    if not config.enabled:
        return False
    if len(messages) > config.max_session_messages:
        return True
    total_chars = sum(len(content) for _, content in messages)
    if total_chars > config.max_context_chars:
        return True
    return False


async def compact_session(
    session_key: str,
    messages: list[tuple[str, str]],
    config: CompactionConfig,
    gemini_executor,
    queue,
    worklog_dir: Path | str | None = None,
) -> CompactionResult:
    """Compact older messages into a summary, keeping recent ones intact.

    Flow:
    1. Split messages into old (to compact) and recent (to keep)
    2. Pre-compaction memory flush: extract durable facts into MEMORY.md
    3. Summarize the old messages using the model
    4. Replace old messages with a single summary message in the DB
    """
    if not needs_compaction(messages, config):
        return CompactionResult(did_compact=False)

    keep_count = min(config.keep_recent, len(messages))
    old_messages = messages[:-keep_count] if keep_count < len(messages) else []
    recent_messages = messages[-keep_count:]

    if len(old_messages) < 4:
        return CompactionResult(did_compact=False)

    if worklog_dir is not None:
        await _flush_to_memory(old_messages, gemini_executor, config, worklog_dir)

    summary = await _summarize_messages(old_messages, gemini_executor, config)
    if not summary:
        return CompactionResult(did_compact=False)

    compact_entry = f"{COMPACTION_MARKER}\n{summary}"

    await queue.clear_session_messages(session_key)

    await queue.add_session_message(
        session_key, "system", compact_entry, task_id=None,
    )
    for role, content in recent_messages:
        await queue.add_session_message(
            session_key, role, content, task_id=None,
        )

    return CompactionResult(
        did_compact=True,
        summary=summary,
        messages_compacted=len(old_messages),
        messages_kept=len(recent_messages),
    )


async def _summarize_messages(
    messages: list[tuple[str, str]],
    gemini_executor,
    config: CompactionConfig,
) -> str:
    conversation_text = _format_messages_for_summary(messages)

    prompt = (
        f"{COMPACTION_SYSTEM_PROMPT}\n\n"
        f"--- Conversation to summarize ({len(messages)} turns) ---\n\n"
        f"{conversation_text}"
    )

    result = await gemini_executor.execute(prompt)
    if result.return_code != 0:
        return ""
    return result.text.strip()


def _format_messages_for_summary(messages: list[tuple[str, str]]) -> str:
    lines = []
    for role, content in messages:
        label = role.capitalize()
        truncated = content[:2000] if len(content) > 2000 else content
        lines.append(f"[{label}]: {truncated}")
    return "\n\n".join(lines)


async def _flush_to_memory(
    messages: list[tuple[str, str]],
    gemini_executor,
    config: CompactionConfig,
    worklog_dir: Path | str,
) -> None:
    """Pre-compaction memory flush: extract durable facts into MEMORY.md."""
    from .tools.memory_ops import memory_write

    conversation_text = _format_messages_for_summary(messages)
    prompt = (
        f"{MEMORY_EXTRACTION_PROMPT}\n\n"
        f"--- Conversation ({len(messages)} turns) ---\n\n"
        f"{conversation_text}"
    )

    result = await gemini_executor.execute(prompt)
    if result.return_code != 0:
        return

    extracted = result.text.strip()
    if not extracted or extracted == "NOTHING_NEW":
        return

    bullet_lines = [
        line.strip() for line in extracted.splitlines()
        if line.strip().startswith("- ")
    ]
    if not bullet_lines:
        return

    memory_write(
        str(worklog_dir),
        section="Key Facts",
        content="\n".join(bullet_lines),
        mode="append",
    )


def extract_compaction_summary(messages: list[tuple[str, str]]) -> Optional[str]:
    """Extract existing compaction summary from the start of messages."""
    if not messages:
        return None
    role, content = messages[0]
    if role == "system" and content.startswith(COMPACTION_MARKER):
        return content[len(COMPACTION_MARKER):].strip()
    return None
