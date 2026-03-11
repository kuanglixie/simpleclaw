"""Memory tools: read and write the agent's persistent MEMORY.md file.

MEMORY.md is a structured Markdown file with sections like Preferences,
Key Facts, Learned Patterns, Projects, and People. The agent uses these
tools to persist important knowledge across sessions and compaction cycles.
"""

from __future__ import annotations

import re
from pathlib import Path

MEMORY_FILENAME = "MEMORY.md"

VALID_SECTIONS = frozenset({
    "Preferences",
    "Key Facts",
    "Learned Patterns",
    "Projects",
    "People",
})

TOKEN_WARNING_CHARS = 12_000  # ~3000 tokens


def _memory_path(worklog_dir: str) -> Path:
    return Path(worklog_dir) / MEMORY_FILENAME


def _ensure_memory_file(path: Path) -> None:
    if not path.exists():
        path.write_text(
            "# Memory\n\nLong-term knowledge accumulated by the agent.\n\n"
            + "".join(f"## {s}\n\n- (none yet)\n\n" for s in VALID_SECTIONS),
        )


def _parse_sections(text: str) -> dict[str, str]:
    """Parse MEMORY.md into {section_name: section_body} pairs."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []

    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+)$", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = heading.group(1).strip()
            lines = []
        elif current is not None:
            lines.append(line)

    if current is not None:
        sections[current] = "\n".join(lines).strip()

    return sections


def _rebuild_file(header: str, sections: dict[str, str]) -> str:
    """Rebuild the full MEMORY.md content from header + sections."""
    parts = [header.rstrip()]
    for name in VALID_SECTIONS:
        body = sections.get(name, "- (none yet)")
        parts.append(f"\n\n## {name}\n\n{body}")
    for name, body in sections.items():
        if name not in VALID_SECTIONS:
            parts.append(f"\n\n## {name}\n\n{body}")
    return "".join(parts) + "\n"


def _extract_header(text: str) -> str:
    """Extract everything before the first ## heading."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^##\s+", line):
            break
        lines.append(line)
    return "\n".join(lines)


def memory_read(worklog_dir: str, section: str = "") -> str:
    """Read MEMORY.md, optionally filtered to a specific section."""
    path = _memory_path(worklog_dir)
    _ensure_memory_file(path)
    text = path.read_text(errors="replace")

    if not section:
        return text

    sections = _parse_sections(text)
    matched = sections.get(section)
    if matched is None:
        for name, body in sections.items():
            if name.lower() == section.lower():
                return f"## {name}\n\n{body}"
        return f"Section '{section}' not found. Available: {', '.join(sections.keys())}"
    return f"## {section}\n\n{matched}"


def memory_write(
    worklog_dir: str,
    section: str,
    content: str,
    mode: str = "append",
) -> str:
    """Write to a section of MEMORY.md.

    Args:
        section: Section name (e.g., "Key Facts", "Preferences").
        content: The content to write.
        mode: "append" adds to the section, "replace" overwrites it.
    """
    if not section:
        return "Error: section name is required."
    if not content.strip():
        return "Error: content cannot be empty."

    path = _memory_path(worklog_dir)
    _ensure_memory_file(path)
    text = path.read_text(errors="replace")

    header = _extract_header(text)
    sections = _parse_sections(text)

    existing = sections.get(section, "")

    if mode == "replace":
        sections[section] = content.strip()
    else:
        cleaned = existing
        if cleaned in ("- (none yet)", "(none yet)", ""):
            cleaned = ""
        if cleaned:
            sections[section] = cleaned + "\n" + content.strip()
        else:
            sections[section] = content.strip()

    new_text = _rebuild_file(header, sections)

    if len(new_text) > TOKEN_WARNING_CHARS:
        warning = (
            f" Warning: MEMORY.md is now {len(new_text)} chars "
            f"(~{len(new_text) // 4} tokens). Consider pruning old entries."
        )
    else:
        warning = ""

    path.write_text(new_text)
    return f"Updated section '{section}' ({mode}).{warning}"
