"""Nightly memory consolidation (autoDream equivalent).

Reviews recent daily notes, promotes lasting facts to MEMORY.md,
deduplicates and prunes stale entries, and archives old daily notes.
Runs via a nightly cron job or the self-improve loop.
"""

from __future__ import annotations

from pathlib import Path

from .tools.daily_notes import archive_old_notes, _memory_dir
from .tools.memory_ops import (
    MEMORY_FILENAME,
    _ensure_memory_file,
    _extract_header,
    _parse_sections,
    _rebuild_file,
)

CONSOLIDATION_PROMPT = """\
You are a memory consolidation assistant. You have two inputs:

1. DAILY NOTES: Raw facts captured over the last few days
2. MEMORY.md: The current curated long-term memory

Your job is to produce an UPDATED MEMORY.md that:

## Rules
- **Promote** lasting facts from daily notes that aren't already in MEMORY.md
- **Merge** duplicate entries — same fact should appear exactly once
- **Remove** clearly stale/outdated entries (completed projects can stay in the \
table as "Done", but don't keep redundant descriptions)
- **Update** entries where daily notes contain newer information
- **Convert** relative dates ("yesterday", "last week") to absolute dates
- **Keep** the existing section structure: Preferences, Key Facts, Projects, \
People, Learned Patterns
- **Respect** the People table format: | Person | Role / Context |
- **Respect** the Projects table format: | # | Name | Status | Notes |
- Do NOT add transient information (job IDs, specific error messages from \
one-off failures, task queue details)
- Do NOT exceed ~120 lines total
- Output ONLY the complete updated MEMORY.md content, no preamble

## What belongs in MEMORY.md vs daily notes
- MEMORY.md: Durable facts, decisions, preferences, people, project status, \
learned patterns — things that matter next week
- Daily notes: Raw observations, transient status, specific job outputs — \
things that matter today only
"""


def _read_recent_notes(worklog_dir: Path, days: int = 7) -> str:
    """Read the last N days of daily notes."""
    mem_dir = _memory_dir(worklog_dir)
    note_files = sorted(mem_dir.glob("????-??-??.md"), reverse=True)[:days]

    if not note_files:
        return ""

    parts = []
    total_chars = 0
    max_chars = 20_000

    for path in note_files:
        text = path.read_text(errors="replace")
        if total_chars + len(text) > max_chars:
            break
        parts.append(text)
        total_chars += len(text)

    return "\n\n---\n\n".join(parts)


def build_consolidation_prompt(worklog_dir: Path) -> str | None:
    """Build the prompt for the consolidation agent.

    Returns None if there are no daily notes to consolidate.
    """
    notes = _read_recent_notes(worklog_dir, days=7)
    if not notes.strip():
        return None

    memory_path = worklog_dir / MEMORY_FILENAME
    _ensure_memory_file(memory_path)
    memory_text = memory_path.read_text(errors="replace")

    return (
        f"{CONSOLIDATION_PROMPT}\n\n"
        f"--- CURRENT MEMORY.md ---\n\n{memory_text}\n\n"
        f"--- DAILY NOTES (last 7 days) ---\n\n{notes}"
    )


async def run_consolidation(
    worklog_dir: Path,
    model: str,
) -> str:
    """Run the full consolidation pipeline.

    1. Build prompt from daily notes + current MEMORY.md
    2. Ask LLM to produce updated MEMORY.md
    3. Write the result (with safety checks)
    4. Archive old daily notes (>30 days)
    """
    from pydantic_ai import Agent
    from pydantic_ai.settings import ModelSettings

    prompt = build_consolidation_prompt(worklog_dir)
    if prompt is None:
        return "No daily notes to consolidate."

    consolidation_agent: Agent[None] = Agent(
        model,
        instructions="You are a memory consolidation assistant.",
        model_settings=ModelSettings(temperature=0.1),
    )

    try:
        result = await consolidation_agent.run(prompt)
        new_memory = str(result.output).strip()
    except Exception as exc:  # noqa: BLE001
        return f"Consolidation failed: {exc}"

    if not new_memory or len(new_memory) < 200:
        return "Consolidation produced empty or too-short output. Skipped."

    if not new_memory.startswith("# Memory"):
        new_memory = "# Memory\n\n" + new_memory

    memory_path = worklog_dir / MEMORY_FILENAME
    backup_path = worklog_dir / f"{MEMORY_FILENAME}.bak"

    current = memory_path.read_text(errors="replace") if memory_path.exists() else ""
    backup_path.write_text(current)

    memory_path.write_text(new_memory + "\n")

    archived = archive_old_notes(worklog_dir, keep_days=30)

    return (
        f"Consolidation complete. "
        f"MEMORY.md: {len(current)} -> {len(new_memory)} chars. "
        f"Archived {archived} old daily notes. "
        f"Backup at {backup_path.name}."
    )
