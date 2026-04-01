"""Tests for the snoocode MCP bridge client."""

import json
import subprocess
from unittest import mock

import pytest

from agent.tools import snoocode


# ---------------------------------------------------------------------------
# SSE response parsing
# ---------------------------------------------------------------------------

class TestParseSSEResult:
    def test_standard_sse_event(self):
        body = 'event: message\ndata: {"result":{"content":[{"type":"text","text":"pong"}]},"jsonrpc":"2.0","id":1}\n\n'
        parsed = snoocode._parse_sse_result(body)
        assert parsed is not None
        assert parsed["result"]["content"][0]["text"] == "pong"

    def test_plain_json_fallback(self):
        body = '{"result":{"content":[{"type":"text","text":"ok"}]},"jsonrpc":"2.0","id":1}'
        parsed = snoocode._parse_sse_result(body)
        assert parsed is not None
        assert parsed["result"]["content"][0]["text"] == "ok"

    def test_empty_data_lines_skipped(self):
        body = "event: message\ndata: \ndata: {\"result\":\"ok\",\"jsonrpc\":\"2.0\",\"id\":1}\n\n"
        parsed = snoocode._parse_sse_result(body)
        assert parsed is not None
        assert parsed["result"] == "ok"

    def test_garbage_returns_none(self):
        assert snoocode._parse_sse_result("not json at all") is None

    def test_empty_string_returns_none(self):
        assert snoocode._parse_sse_result("") is None


# ---------------------------------------------------------------------------
# call_tool — result extraction
# ---------------------------------------------------------------------------

class TestCallToolResultExtraction:
    """Test call_tool's parsing of different MCP response shapes."""

    def _mock_curl(self, sse_body: str):
        """Return a mock that simulates curl returning an SSE response."""
        result = mock.MagicMock()
        result.returncode = 0
        result.stdout = sse_body
        result.stderr = ""
        return mock.patch("subprocess.run", return_value=result)

    def setup_method(self):
        snoocode._session_id = "fake-session-id"

    def teardown_method(self):
        snoocode._session_id = None

    def test_extracts_text_content(self):
        sse = 'data: {"result":{"content":[{"type":"text","text":"hello world"}]},"jsonrpc":"2.0","id":1}\n'
        with self._mock_curl(sse):
            assert snoocode.call_tool("test_tool") == "hello world"

    def test_extracts_multiple_text_parts(self):
        sse = 'data: {"result":{"content":[{"type":"text","text":"line1"},{"type":"text","text":"line2"}]},"jsonrpc":"2.0","id":1}\n'
        with self._mock_curl(sse):
            assert snoocode.call_tool("test_tool") == "line1\nline2"

    def test_returns_error_on_jsonrpc_error(self):
        sse = 'data: {"error":{"code":-32000,"message":"tool not found"},"jsonrpc":"2.0","id":1}\n'
        with self._mock_curl(sse):
            result = snoocode.call_tool("missing_tool")
            assert result.startswith("Error:")
            assert "tool not found" in result

    def test_returns_string_result_directly(self):
        sse = 'data: {"result":"plain string","jsonrpc":"2.0","id":1}\n'
        with self._mock_curl(sse):
            assert snoocode.call_tool("test_tool") == "plain string"

    def test_returns_json_for_dict_without_content(self):
        sse = 'data: {"result":{"status":"ok","count":5},"jsonrpc":"2.0","id":1}\n'
        with self._mock_curl(sse):
            result = snoocode.call_tool("test_tool")
            parsed = json.loads(result)
            assert parsed["status"] == "ok"

    def test_empty_response_resets_session(self):
        result = mock.MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        with mock.patch("subprocess.run", return_value=result):
            out = snoocode.call_tool("test_tool")
            assert "Error" in out
            assert snoocode._session_id is None

    def test_session_error_resets_session(self):
        sse = 'data: {"error":{"code":-32000,"message":"No valid session"},"jsonrpc":"2.0","id":null}\n'
        with self._mock_curl(sse):
            out = snoocode.call_tool("test_tool")
            assert "Error" in out
            assert snoocode._session_id is None


# ---------------------------------------------------------------------------
# Session initialization
# ---------------------------------------------------------------------------

class TestEnsureSession:
    def setup_method(self):
        snoocode._session_id = None

    def teardown_method(self):
        snoocode._session_id = None

    def test_extracts_session_id_from_headers(self):
        header_response = (
            "HTTP/1.1 200 OK\r\n"
            "mcp-session-id: test-session-abc\r\n"
            "content-type: text/event-stream\r\n"
            "\r\n"
            'event: message\ndata: {"result":{"protocolVersion":"2025-03-26"},"jsonrpc":"2.0","id":1}\n'
        )
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            m = mock.MagicMock()
            m.returncode = 0
            m.stderr = ""
            if call_count == 1:
                m.stdout = header_response
            else:
                m.stdout = ""
            return m

        with mock.patch("subprocess.run", side_effect=side_effect):
            sid = snoocode._ensure_session()
            assert sid == "test-session-abc"
            assert snoocode._session_id == "test-session-abc"
            assert call_count == 2  # init + notifications/initialized

    def test_returns_cached_session(self):
        snoocode._session_id = "already-set"
        with mock.patch("subprocess.run") as mock_run:
            sid = snoocode._ensure_session()
            assert sid == "already-set"
            mock_run.assert_not_called()

    def test_returns_none_on_failed_init(self):
        result = mock.MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "connection refused"
        with mock.patch("subprocess.run", return_value=result):
            sid = snoocode._ensure_session()
            assert sid is None


# ---------------------------------------------------------------------------
# Convenience wrappers pass correct arguments
# ---------------------------------------------------------------------------

class TestConvenienceWrappers:
    def setup_method(self):
        snoocode._session_id = "fake-session"

    def teardown_method(self):
        snoocode._session_id = None

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_ping(self, mock_call):
        snoocode.ping()
        mock_call.assert_called_once_with("snoocode_ping")

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_list_prs(self, mock_call):
        snoocode.list_prs("reddit", "my-repo", state="closed")
        mock_call.assert_called_once_with(
            "list_prs", {"owner": "reddit", "repo": "my-repo", "state": "closed"},
        )

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_ci_watcher(self, mock_call):
        snoocode.ci_watcher("reddit", "my-repo", 42, poll_interval=10, timeout=300)
        mock_call.assert_called_once_with("ci_watcher", {
            "owner": "reddit", "repo": "my-repo", "pull_number": 42,
            "poll_interval": 10, "timeout": 300,
        })

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_run_agent_minimal(self, mock_call):
        snoocode.run_agent("do something")
        mock_call.assert_called_once_with("run_agent", {"prompt": "do something"})

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_run_agent_full(self, mock_call):
        snoocode.run_agent("fix it", workspace="/tmp/repo", model="fast")
        mock_call.assert_called_once_with("run_agent", {
            "prompt": "fix it", "workspace": "/tmp/repo", "model": "fast",
        })

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_get_ticket(self, mock_call):
        snoocode.get_ticket("DISCO-1234")
        mock_call.assert_called_once_with("get_ticket", {"ticket_key": "DISCO-1234"})

    @mock.patch("agent.tools.snoocode.call_tool", return_value="ok")
    def test_add_comment(self, mock_call):
        snoocode.add_comment("DISCO-1234", "looks good")
        mock_call.assert_called_once_with(
            "add_comment", {"ticket_key": "DISCO-1234", "body": "looks good"},
        )
