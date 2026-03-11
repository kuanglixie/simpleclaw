"""Gemini API client with native function calling support.

Supports both simple text generation and multi-turn conversations
with tool use via the google-genai SDK's function calling protocol.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Optional

from google import genai
from google.genai import types


@dataclass
class FunctionCall:
    name: str
    args: dict


@dataclass
class GeminiResult:
    text: str
    stderr: str
    return_code: int
    function_calls: list[FunctionCall] = field(default_factory=list)
    raw_content: Any = None


@dataclass
class VertexConfig:
    enabled: bool = False
    project: str = ""
    location: str = "us-central1"


class GeminiExecutor:
    def __init__(
        self,
        model: str,
        timeout_seconds: int = 300,
        vertex: VertexConfig | None = None,
        tool_declarations: list[dict] | None = None,
        gemini_bin: str = "",
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.vertex = vertex or VertexConfig()
        self.tool_declarations = tool_declarations
        self._client = self._build_client()
        self._tools = self._build_tools()

    def _build_client(self) -> genai.Client:
        if self.vertex.enabled:
            return genai.Client(
                vertexai=True,
                project=self.vertex.project or None,
                location=self.vertex.location or "us-central1",
            )
        return genai.Client()

    def _build_tools(self) -> Optional[list[types.Tool]]:
        if not self.tool_declarations:
            return None
        return [types.Tool(function_declarations=self.tool_declarations)]

    async def execute(self, prompt: str) -> GeminiResult:
        """Simple text generation (legacy, no tools)."""
        loop = asyncio.get_running_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, partial(self._call_sync, prompt)),
                timeout=self.timeout_seconds,
            )
            return response
        except asyncio.TimeoutError:
            return GeminiResult(
                text="",
                stderr=f"Gemini execution timed out after {self.timeout_seconds}s",
                return_code=124,
            )
        except Exception as exc:
            return GeminiResult(text="", stderr=str(exc), return_code=1)

    async def generate_with_tools(
        self,
        contents: list,
        system_instruction: str = "",
    ) -> GeminiResult:
        """Generate with native function calling. Returns function calls or text."""
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    partial(self._call_with_tools_sync, contents, system_instruction),
                ),
                timeout=self.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            return GeminiResult(
                text="",
                stderr=f"Gemini timed out after {self.timeout_seconds}s",
                return_code=124,
            )
        except Exception as exc:
            return GeminiResult(text="", stderr=str(exc), return_code=1)

    def _call_sync(self, prompt: str) -> GeminiResult:
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = response.text or ""
        return GeminiResult(text=text, stderr="", return_code=0)

    def _call_with_tools_sync(
        self, contents: list, system_instruction: str,
    ) -> GeminiResult:
        config = types.GenerateContentConfig(
            temperature=0.2,
            tools=self._tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
        )
        if system_instruction:
            config.system_instruction = system_instruction

        response = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        if not response.candidates:
            return GeminiResult(
                text="", stderr="No candidates in response", return_code=1,
            )

        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return GeminiResult(
                text="", stderr="Empty response from model", return_code=1,
            )

        fn_calls = []
        text_parts = []
        for part in candidate.content.parts:
            if part.function_call:
                fn_calls.append(FunctionCall(
                    name=part.function_call.name,
                    args=dict(part.function_call.args) if part.function_call.args else {},
                ))
            elif part.text:
                text_parts.append(part.text)

        return GeminiResult(
            text="\n".join(text_parts),
            stderr="",
            return_code=0,
            function_calls=fn_calls,
            raw_content=candidate.content,
        )
