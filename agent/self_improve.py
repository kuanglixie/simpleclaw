"""Self-improvement: daily retrospective that mines task history for patterns.

Runs as a cron job (default: 11pm daily). It:
1. Reads today's completed/failed tasks from the outbox
2. Extracts signals: failure patterns, slow tasks, tool usage, user corrections
3. Produces an evolution report (EVOLUTION_LOG.md) with concrete improvements
4. Updates MEMORY.md "Learned Patterns" with new insights
5. Optionally proposes prompt/config changes via cursor_agent

The loop is designed to be safe: it only appends to MEMORY.md and logs,
never modifies source code autonomously without user approval.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class TaskSignal:
    task_id: str
    source: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    tool_names: list[str]
    error_count: int
    errors: list[str]
    summary_snippet: str


@dataclass
class DailyDigest:
    date: str
    total_tasks: int
    completed: int
    failed: int
    avg_duration_seconds: float
    slowest_task: Optional[TaskSignal]
    failure_patterns: list[str]
    most_used_tools: list[tuple[str, int]]
    improvement_ideas: list[str]
    signals: list[TaskSignal] = field(default_factory=list)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _duration_seconds(started: str, finished: str) -> float:
    s = _parse_iso(started)
    f = _parse_iso(finished)
    if s and f:
        return max(0, (f - s).total_seconds())
    return 0.0


def _extract_signal(data: dict) -> TaskSignal:
    tool_calls = data.get("tool_calls", [])
    tool_names = [tc.get("name", "unknown") for tc in tool_calls]
    errors = data.get("errors", []) or []
    summary = data.get("summary", "") or ""
    return TaskSignal(
        task_id=data.get("task_id", ""),
        source=data.get("source", ""),
        status=data.get("status", ""),
        started_at=data.get("started_at", ""),
        finished_at=data.get("finished_at", ""),
        duration_seconds=_duration_seconds(
            data.get("started_at", ""), data.get("finished_at", ""),
        ),
        tool_names=tool_names,
        error_count=len(errors),
        errors=[e[:200] for e in errors[:5]],
        summary_snippet=summary[:200],
    )


def collect_day_signals(outbox_dir: Path, target_date: str) -> list[TaskSignal]:
    """Scan outbox JSON files for tasks that ran on `target_date` (YYYY-MM-DD)."""
    signals: list[TaskSignal] = []
    if not outbox_dir.exists():
        return signals

    for json_path in outbox_dir.glob("*.json"):
        try:
            data = json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        started = data.get("started_at", "")
        if not started or not started.startswith(target_date):
            finished = data.get("finished_at", "")
            if not finished or not finished.startswith(target_date):
                continue

        signals.append(_extract_signal(data))
    return signals


def _find_failure_patterns(signals: list[TaskSignal]) -> list[str]:
    """Group failures by common error substrings."""
    error_buckets: dict[str, int] = {}
    for sig in signals:
        if sig.status != "failed":
            continue
        for err in sig.errors:
            key = _normalize_error(err)
            if key:
                error_buckets[key] = error_buckets.get(key, 0) + 1

    return [
        f"{pattern} (x{count})"
        for pattern, count in sorted(error_buckets.items(), key=lambda x: -x[1])
        if count > 0
    ]


def _normalize_error(err: str) -> str:
    """Extract the core error type from a message."""
    err = err.strip()
    match = re.match(r"((?:\w+Error|\w+Exception|Timeout|Quota\w+):\s*.{10,80})", err)
    if match:
        return match.group(1).strip()
    if len(err) > 20:
        return err[:80]
    return ""


def _count_tools(signals: list[TaskSignal]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for sig in signals:
        for name in sig.tool_names:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:15]


def _generate_improvement_ideas(
    signals: list[TaskSignal],
    failure_patterns: list[str],
    tool_counts: list[tuple[str, int]],
) -> list[str]:
    """Heuristic-based improvement suggestions from today's data."""
    ideas: list[str] = []

    failed = [s for s in signals if s.status == "failed"]
    if len(failed) > 3:
        ideas.append(
            f"High failure rate today ({len(failed)}/{len(signals)} tasks). "
            "Review error patterns and consider adding retry logic or better error handling."
        )

    slow = [s for s in signals if s.duration_seconds > 120]
    if slow:
        names = ", ".join(s.source for s in slow[:3])
        ideas.append(
            f"{len(slow)} tasks took >2 min. Slowest sources: {names}. "
            "Consider breaking into smaller steps or adding timeouts."
        )

    heartbeat_fails = [
        s for s in signals
        if "heartbeat" in s.source.lower() and s.status == "failed"
    ]
    if heartbeat_fails:
        ideas.append(
            f"Heartbeat failed {len(heartbeat_fails)} time(s). "
            "Check if cursor_agent delegation is timing out or if the prompt is too complex."
        )

    cron_fails = [
        s for s in signals
        if s.source.startswith("cron:") and s.status == "failed"
    ]
    if cron_fails:
        job_names = set(s.source for s in cron_fails)
        ideas.append(
            f"Cron jobs failed: {', '.join(job_names)}. "
            "Check job definitions in jobs.json and their prompts."
        )

    tool_names = {name for name, _ in tool_counts}
    if "cursor_agent" not in tool_names and len(signals) > 5:
        ideas.append(
            "cursor_agent was not used today. For complex tasks, "
            "delegating to cursor_agent could improve quality."
        )

    for pattern in failure_patterns[:3]:
        if "timeout" in pattern.lower():
            ideas.append(f"Recurring timeout: {pattern}. Increase timeout or simplify the task.")
        elif "quota" in pattern.lower():
            ideas.append(f"Quota issue: {pattern}. Consider rate limiting or model fallback.")

    if not ideas:
        ideas.append("No significant issues detected. System operating normally.")

    return ideas


def build_daily_digest(outbox_dir: Path, target_date: str) -> DailyDigest:
    """Build a full digest for a given date."""
    signals = collect_day_signals(outbox_dir, target_date)

    completed = [s for s in signals if s.status == "completed"]
    failed = [s for s in signals if s.status == "failed"]
    durations = [s.duration_seconds for s in signals if s.duration_seconds > 0]
    avg_dur = sum(durations) / len(durations) if durations else 0.0
    slowest = max(signals, key=lambda s: s.duration_seconds, default=None)
    failure_patterns = _find_failure_patterns(signals)
    tool_counts = _count_tools(signals)
    ideas = _generate_improvement_ideas(signals, failure_patterns, tool_counts)

    return DailyDigest(
        date=target_date,
        total_tasks=len(signals),
        completed=len(completed),
        failed=len(failed),
        avg_duration_seconds=avg_dur,
        slowest_task=slowest,
        failure_patterns=failure_patterns,
        most_used_tools=tool_counts,
        improvement_ideas=ideas,
        signals=signals,
    )


def format_digest_markdown(digest: DailyDigest) -> str:
    """Render a digest as a markdown section for EVOLUTION_LOG.md."""
    lines = [
        f"## {digest.date}",
        "",
        f"**Tasks:** {digest.total_tasks} total, "
        f"{digest.completed} completed, {digest.failed} failed",
        f"**Avg duration:** {digest.avg_duration_seconds:.0f}s",
    ]

    if digest.slowest_task:
        lines.append(
            f"**Slowest:** {digest.slowest_task.source} "
            f"({digest.slowest_task.duration_seconds:.0f}s)"
        )

    if digest.most_used_tools:
        top5 = ", ".join(f"{n}({c})" for n, c in digest.most_used_tools[:5])
        lines.append(f"**Top tools:** {top5}")

    if digest.failure_patterns:
        lines.append("")
        lines.append("### Failure Patterns")
        for p in digest.failure_patterns:
            lines.append(f"- {p}")

    lines.append("")
    lines.append("### Improvement Ideas")
    for idea in digest.improvement_ideas:
        lines.append(f"- {idea}")

    lines.append("")
    return "\n".join(lines)


def format_digest_telegram(digest: DailyDigest) -> str:
    """Compact digest for Telegram notification."""
    parts = [
        f"**Daily Self-Review ({digest.date})**",
        f"{digest.completed}/{digest.total_tasks} tasks OK, {digest.failed} failed",
    ]

    if digest.avg_duration_seconds > 0:
        parts.append(f"Avg {digest.avg_duration_seconds:.0f}s per task")

    if digest.failure_patterns:
        parts.append(f"\n**Issues:** {'; '.join(digest.failure_patterns[:3])}")

    if digest.improvement_ideas and digest.improvement_ideas[0] != \
            "No significant issues detected. System operating normally.":
        parts.append("\n**Ideas:**")
        for idea in digest.improvement_ideas[:3]:
            parts.append(f"- {idea}")

    return "\n".join(parts)


def append_evolution_log(worklog_dir: Path, digest: DailyDigest) -> Path:
    """Append today's digest to EVOLUTION_LOG.md."""
    log_path = worklog_dir / "EVOLUTION_LOG.md"
    md = format_digest_markdown(digest)

    if log_path.exists():
        existing = log_path.read_text(errors="replace")
        if digest.date in existing:
            return log_path
        log_path.write_text(existing.rstrip() + "\n\n" + md + "\n")
    else:
        header = (
            "# SimpleClaw Evolution Log\n\n"
            "Auto-generated daily self-improvement reports.\n"
            "Each entry summarizes task performance, failure patterns, and improvement ideas.\n\n"
        )
        log_path.write_text(header + md + "\n")

    return log_path


def extract_memory_updates(digest: DailyDigest) -> list[str]:
    """Derive memory-worthy patterns from the digest.

    Only returns entries if there's something genuinely new to learn.
    """
    entries: list[str] = []
    today = digest.date

    if digest.failed > 0 and digest.total_tasks > 0:
        rate = digest.failed / digest.total_tasks
        if rate > 0.3:
            entries.append(
                f"[{today}] High failure rate ({digest.failed}/{digest.total_tasks}). "
                f"Common errors: {'; '.join(digest.failure_patterns[:2]) or 'various'}. "
                f"Investigate root causes."
            )

    for idea in digest.improvement_ideas:
        if any(kw in idea.lower() for kw in ("timeout", "quota", "heartbeat failed")):
            entries.append(f"[{today}] {idea}")

    if digest.avg_duration_seconds > 90 and digest.total_tasks > 5:
        entries.append(
            f"[{today}] Average task duration {digest.avg_duration_seconds:.0f}s "
            f"is high. Consider optimizing tool chains or simplifying prompts."
        )

    return entries


SELF_IMPROVE_PROMPT = """\
DAILY SELF-IMPROVEMENT REVIEW. You have access to today's performance data.

Use cursor_agent() to investigate and propose improvements:

1. Review EVOLUTION_LOG.md for today's digest (task success/failure rates, patterns)
2. Check MEMORY.md "Learned Patterns" for recurring issues
3. If there are repeated failures or slow patterns, investigate the root cause
4. Look at the system prompt and heartbeat prompt — are they causing issues?
5. Check if any cron jobs need schedule adjustments

After investigation, update MEMORY.md "Learned Patterns" with NEW insights only.
Do NOT repeat patterns already recorded.

If you find a concrete code improvement (bug fix, better error handling, prompt tweak):
- Describe it clearly in your response
- Note the file and what should change
- Do NOT apply code changes automatically — flag them for user review

Format your response for Telegram:
- Lead with key finding
- List improvements as bullet points
- End with "Proposed changes:" if any code changes are recommended
- Reply HEARTBEAT_OK if nothing actionable found
"""
