"""状态机转换表测试（§15.1）。"""
import pytest

from app.modules.competitions.statemachine import can_transition


def test_happy_path():
    path = ["draft", "lobby", "running", "locked", "calculated",
            "reveal_scheduled", "revealed", "archived"]
    for a, b in zip(path, path[1:]):
        assert can_transition(a, b), f"{a}→{b} 应合法"


def test_pause_and_cancel():
    assert can_transition("running", "paused")
    assert can_transition("paused", "running")
    assert can_transition("paused", "locked")
    for s in ("draft", "lobby", "running", "paused"):
        assert can_transition(s, "cancelled")


def test_illegal_paths():
    assert not can_transition("draft", "running")          # 必须先 lobby
    assert not can_transition("locked", "running")         # 锁定后不能回退
    assert not can_transition("calculated", "running")
    assert not can_transition("revealed", "calculated")
    assert not can_transition("archived", "archived")
    assert not can_transition("cancelled", "lobby")
    assert not can_transition("nope", "lobby")  # 未知状态一律拒绝
