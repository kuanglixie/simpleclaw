"""File operation tools: read, write, edit."""

from __future__ import annotations

from pathlib import Path


def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: file not found: {path}"
    if not p.is_file():
        return f"Error: not a file: {path}"

    text = p.read_text(errors="replace")
    lines = text.splitlines()

    if offset > 0 or limit > 0:
        start = max(0, offset - 1)  # 1-indexed to 0-indexed
        end = start + limit if limit > 0 else len(lines)
        selected = lines[start:end]
        numbered = [f"{start + i + 1:6d}|{line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    if len(text) > 100_000:
        return (
            f"File has {len(lines)} lines ({len(text)} bytes). "
            "Use offset/limit to read portions. First 200 lines:\n"
            + "\n".join(f"{i + 1:6d}|{line}" for i, line in enumerate(lines[:200]))
        )

    return text


def write_file(path: str, contents: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contents)
    lines = contents.count("\n") + (1 if contents and not contents.endswith("\n") else 0)
    return f"Wrote {len(contents)} bytes ({lines} lines) to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: file not found: {path}"

    text = p.read_text(errors="replace")
    count = text.count(old_string)
    if count == 0:
        snippet = old_string[:120]
        return f"Error: old_string not found in {path}. Searched for: {snippet!r}"
    if count > 1:
        return (
            f"Error: old_string matches {count} locations in {path}. "
            "Provide more context to make it unique."
        )

    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text)
    old_lines = old_string.count("\n") + 1
    new_lines = new_string.count("\n") + 1
    return f"Replaced {old_lines} lines with {new_lines} lines in {path}"
