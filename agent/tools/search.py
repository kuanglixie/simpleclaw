"""Search tools: glob and grep (ripgrep)."""

from __future__ import annotations

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
