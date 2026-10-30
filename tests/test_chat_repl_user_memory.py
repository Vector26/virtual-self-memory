from __future__ import annotations

from uuid import uuid4

import pytest

from virtual_self.auth.credentials import AuthContext, resolve_auth
from virtual_self.cli.chat_repl import (
    UserMemoryPlan,
    _apply_user_memory_plan,
    user_memory_plan_for_turn,
)
from virtual_self.config import get_settings


def test_plan_none_for_explicit_remember():
    assert user_memory_plan_for_turn("remember that Nebraska has hail") == UserMemoryPlan("none")


def test_plan_people_in_my_life_inline():
    plan = user_memory_plan_for_turn("Akshansh is one of the people in my life")
    assert plan.action == "inline"
    assert plan.kind == "relationship"
    assert "Akshansh" in (plan.content or "")


def test_plan_should_know_extract_user_turn():
    assert user_memory_plan_for_turn(
        "You should know that my sister lives in Omaha"
    ) == UserMemoryPlan("extract_user")


def test_plan_embedded_remember_that_inline():
    plan = user_memory_plan_for_turn("Also remember that I prefer tea over coffee.")
    assert plan.action == "inline"
    assert plan.content == "I prefer tea over coffee"


def test_plan_casual_line_none():
    assert user_memory_plan_for_turn("what's the weather like?") == UserMemoryPlan("none")


@pytest.fixture
def auth(ensure_db):
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    assert token is not None
    return resolve_auth(f"Bearer {token.get_secret_value()}")


def test_apply_user_memory_extract_user_defers_enqueue(auth, monkeypatch):
    monkeypatch.setattr(
        "virtual_self.cli.chat_repl.enqueue_chat_memory_extract",
        lambda *a, **k: pytest.fail("enqueue happens after user turn in run_chat"),
    )
    skip, note = _apply_user_memory_plan(
        auth,
        uuid4(),
        uuid4(),
        UserMemoryPlan("extract_user"),
    )
    assert skip is False
    assert note == ""


def test_apply_user_memory_casual_turn_defers_enqueue(auth, monkeypatch):
    monkeypatch.setattr(
        "virtual_self.cli.chat_repl.enqueue_chat_memory_extract",
        lambda *a, **k: pytest.fail("enqueue happens after user turn in run_chat"),
    )
    skip, note = _apply_user_memory_plan(
        auth,
        uuid4(),
        uuid4(),
        UserMemoryPlan("none"),
    )
    assert skip is False
    assert note == ""


def test_apply_user_memory_inline_skips_enqueue(auth, monkeypatch):
    monkeypatch.setattr(
        "virtual_self.cli.chat_repl.enqueue_chat_memory_extract",
        lambda *a, **k: pytest.fail("should not enqueue"),
    )
    turn_id = uuid4()
    skip, note = _apply_user_memory_plan(
        auth,
        uuid4(),
        turn_id,
        UserMemoryPlan("inline", content="test inline fact", kind="fact"),
    )
    assert skip is True
    assert "[Memory saved: test inline fact]" in note
