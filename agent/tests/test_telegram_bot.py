"""Tests for Telegram bot message handling — quoted/reply message extraction."""

from __future__ import annotations

from typing import Optional
from unittest import mock

import pytest

from agent.telegram_bot import _extract_quoted_context, _QUOTE_MAX_CHARS


def _make_update(
    text: str = "hello",
    reply_text: Optional[str] = None,
    reply_caption: Optional[str] = None,
    quote_text: Optional[str] = None,
):
    """Build a minimal mock Update with optional reply_to_message and quote."""
    update = mock.MagicMock()
    update.message.text = text

    # quote (highlighted portion)
    if quote_text is not None:
        update.message.quote.text = quote_text
    else:
        update.message.quote = None

    # reply_to_message
    if reply_text is not None or reply_caption is not None:
        update.message.reply_to_message.text = reply_text
        update.message.reply_to_message.caption = reply_caption
    else:
        update.message.reply_to_message = None

    return update


class TestExtractQuotedContext:
    def test_no_quote_no_reply(self):
        update = _make_update("just a message")
        assert _extract_quoted_context(update) == ""

    def test_reply_to_text_message(self):
        update = _make_update("my response", reply_text="original message here")
        assert _extract_quoted_context(update) == "original message here"

    def test_reply_to_caption(self):
        update = _make_update("nice pic", reply_text=None, reply_caption="photo caption")
        assert _extract_quoted_context(update) == "photo caption"

    def test_reply_text_preferred_over_caption(self):
        update = _make_update("reply", reply_text="text wins", reply_caption="caption loses")
        assert _extract_quoted_context(update) == "text wins"

    def test_quote_preferred_over_reply(self):
        update = _make_update(
            "my comment",
            reply_text="full original message that is very long",
            quote_text="just the highlighted part",
        )
        assert _extract_quoted_context(update) == "just the highlighted part"

    def test_long_reply_truncated(self):
        long_text = "x" * (_QUOTE_MAX_CHARS + 500)
        update = _make_update("short reply", reply_text=long_text)
        result = _extract_quoted_context(update)
        assert len(result) <= _QUOTE_MAX_CHARS + 3  # +3 for "..."
        assert result.endswith("...")

    def test_long_quote_truncated(self):
        long_quote = "q" * (_QUOTE_MAX_CHARS + 100)
        update = _make_update("reply", quote_text=long_quote)
        result = _extract_quoted_context(update)
        assert len(result) == _QUOTE_MAX_CHARS

    def test_empty_reply_text(self):
        update = _make_update("my text", reply_text="", reply_caption="")
        assert _extract_quoted_context(update) == ""

    def test_whitespace_reply_text(self):
        update = _make_update("my text", reply_text="   \n  ")
        assert _extract_quoted_context(update) == ""

    def test_none_message(self):
        update = mock.MagicMock()
        update.message = None
        assert _extract_quoted_context(update) == ""
