"""Tool package for the worklog agent.

Provides Claude Code-like tools via native Gemini function calling.
"""

from .executor import ToolExecutor
from .declarations import get_all_declarations

__all__ = ["ToolExecutor", "get_all_declarations"]
