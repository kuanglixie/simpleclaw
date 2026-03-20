"""Search tools: glob, grep (ripgrep), and Cursor conversation search."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def glob_files(pattern: str, directory: str = ".") -> str:
    d = Path(directory).expanduser().resolve()
    if not d.is_dir():
        return f"Error: directory not found: {directory}"

    if not pattern.startswith("**/") and not pattern.startswith("/"):
        pattern = f"**/{pattern}"

    matches = sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)

    if not matches:
        return f"No files matching '{pattern}' in {d}"

    max_results = 100
    lines = [str(m) for m in matches[:max_results]]
    header = f"Found {len(matches)} files"
    if len(matches) > max_results:
        header += f" (showing first {max_results})"
    return header + ":\n" + "\n".join(lines)


def grep_search(
    pattern: str,
    path: str = ".",
    glob_filter: str = "",
    max_results: int = 50,
) -> str:
    cmd = ["rg", "--no-heading", "--line-number", "--max-count", "5"]

    if glob_filter:
        cmd.extend(["--glob", glob_filter])

    cmd.extend(["--max-filesize", "1M"])
    cmd.extend([pattern, str(Path(path).expanduser().resolve())])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return "Error: ripgrep (rg) not found. Install with: brew install ripgrep"

    output = proc.stdout.strip()
    if not output:
        if proc.returncode == 1:
            return f"No matches for pattern '{pattern}' in {path}"
        if proc.stderr:
            return f"Error: {proc.stderr.strip()}"
        return f"No matches for pattern '{pattern}' in {path}"

    lines = output.splitlines()
    if len(lines) > max_results:
        return "\n".join(lines[:max_results]) + f"\n... ({len(lines) - max_results} more matches)"
    return output


def search_conversations(
    query: str,
    worklog_dir: str = ".",
    max_results: int = 10,
) -> str:
    """Search Cursor conversation transcripts by keyword.

    Strategy: grep the .jsonl files for the query, then group matches
    by conversation UUID and return the top hits with context from index.md.
    """
    conv_dir = Path(worklog_dir) / "cursor-conversations"
    if not conv_dir.is_dir():
        return "Error: cursor-conversations/ directory not found."

    index_path = conv_dir / "index.md"
    index_lookup: dict[str, str] = {}
    if index_path.exists():
        for line in index_path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("- ") and "`" in line:
                try:
                    uuid = line.split("`")[1]
                    summary = line.split("` — ", 1)[1] if "` — " in line else "(no summary)"
                    index_lookup[uuid] = summary
                except (IndexError, ValueError):
                    pass

    cmd = [
        "rg", "--no-heading", "--files-with-matches",
        "--max-filesize", "2M", "--glob", "*.jsonl",
        query, str(conv_dir),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return "Error: ripgrep (rg) not found."

    if not proc.stdout.strip():
        return f"No conversations matching '{query}'."

    hit_files = proc.stdout.strip().splitlines()[:max_results]
    results = []
    for fpath in hit_files:
        p = Path(fpath)
        uuid = p.stem
        summary = index_lookup.get(uuid, "(no summary in index)")

        snippet = ""
        try:
            for raw_line in p.read_text(errors="replace").splitlines():
                if query.lower() in raw_line.lower():
                    try:
                        obj = json.loads(raw_line)
                        role = obj.get("role", "?")
                        msg = obj.get("message", {})
                        content_parts = msg.get("content", [])
                        text = ""
                        for part in content_parts:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text", "")
                                break
                        if text:
                            idx = text.lower().find(query.lower())
                            start = max(0, idx - 80)
                            end = min(len(text), idx + len(query) + 120)
                            snippet = f"  [{role}] ...{text[start:end]}..."
                            break
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

        entry = f"- {uuid}\n  {summary}"
        if snippet:
            entry += f"\n{snippet}"
        results.append(entry)

    header = f"Found {len(hit_files)} conversation(s) matching '{query}':\n"
    return header + "\n".join(results)
