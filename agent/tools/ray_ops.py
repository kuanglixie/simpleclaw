"""Ray job management tools."""

from __future__ import annotations

import subprocess
from pathlib import Path


def check_ray_jobs(worklog_dir: str, mode: str = "standalone", top: int = 10) -> str:
    script = Path(worklog_dir) / "scripts" / "ray" / "ray_jobs_inspect.py"
    if not script.exists():
        return _fallback_gazette_list()

    cmd = f"python3 {script} --mode {mode} --top {top} --simple"
    try:
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True, text=True,
            timeout=120, check=False,
            cwd=str(worklog_dir),
        )
    except subprocess.TimeoutExpired:
        return "Ray job check timed out after 120s."

    output = (proc.stdout or "").strip()
    if proc.stderr:
        output += "\n" + proc.stderr.strip()
    return output or "(no ray job output)"


def _fallback_gazette_list() -> str:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "gazette ray job list"],
            capture_output=True, text=True,
            timeout=60, check=False,
        )
        return (proc.stdout or "").strip() + ("\n" + proc.stderr.strip() if proc.stderr else "")
    except Exception as exc:
        return f"Failed to list ray jobs: {exc}"


def ray_job_describe(job_id: str) -> str:
    cmd = f"gazette ray job describe {job_id}"
    try:
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            capture_output=True, text=True,
            timeout=60, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Timed out describing job {job_id}."
    output = (proc.stdout or "").strip()
    if proc.stderr:
        output += "\n" + proc.stderr.strip()
    return output or f"No output for job {job_id}"
