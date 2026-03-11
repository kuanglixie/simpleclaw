from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ToolCallRecord:
    name: str
    input: str
    output_excerpt: str
    exit_code: int
    duration_ms: int


@dataclass
class ArtifactRecord:
    path: str
    description: str


@dataclass
class OutputContract:
    task_id: str
    run_id: str
    attempt: int
    source: str
    session_key: str
    status: str
    started_at: str
    finished_at: str
    model: str
    final_answer: str
    summary: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=True)

    def to_markdown(self) -> str:
        lines = [
            "---",
            f"task_id: {self.task_id}",
            f"run_id: {self.run_id}",
            f"attempt: {self.attempt}",
            f"source: {self.source}",
            f"session_key: {self.session_key}",
            f"status: {self.status}",
            f"started_at: {self.started_at}",
            f"finished_at: {self.finished_at}",
            f"model: {self.model}",
            "---",
            "",
            "## Summary",
            "",
            self.summary or "(no summary)",
            "",
            "## Final Answer",
            "",
            self.final_answer or "(no output)",
            "",
            "## Next Actions",
            "",
        ]
        if self.next_actions:
            lines.extend([f"- {item}" for item in self.next_actions])
        else:
            lines.append("- (none)")

        lines.extend(["", "## Tool Calls", ""])
        if self.tool_calls:
            for call in self.tool_calls:
                lines.append(
                    f"- `{call.name}` exit={call.exit_code} duration_ms={call.duration_ms}"
                )
                lines.append(f"  - input: `{call.input}`")
                if call.output_excerpt:
                    lines.append(f"  - output_excerpt: {call.output_excerpt}")
        else:
            lines.append("- (none)")

        lines.extend(["", "## Errors", ""])
        if self.errors:
            lines.extend([f"- {err}" for err in self.errors])
        else:
            lines.append("- (none)")
        lines.append("")
        return "\n".join(lines)


def contract_paths(outbox_dir: Path, task_id: str) -> tuple[Path, Path]:
    return outbox_dir / f"{task_id}.md", outbox_dir / f"{task_id}.json"


def write_contract(contract: OutputContract, outbox_dir: Path) -> tuple[Path, Path]:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    md_path, json_path = contract_paths(outbox_dir, contract.task_id)
    md_path.write_text(contract.to_markdown())
    json_path.write_text(contract.to_json())
    return md_path, json_path

