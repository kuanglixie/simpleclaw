"""Tests for PATH augmentation under minimal daemon-style environments."""

from __future__ import annotations

import os

import pytest


def test_augment_process_path_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call does not duplicate PATH entries."""
    from agent.config import augment_process_path_for_cli_tools

    monkeypatch.setenv("PATH", "/usr/bin")
    augment_process_path_for_cli_tools()
    first = os.environ["PATH"]
    augment_process_path_for_cli_tools()
    assert os.environ["PATH"] == first
