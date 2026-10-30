from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from virtual_self.archive.detect import classify_file as detect_classify
from virtual_self.archive.llm.specialists import (
    PRODUCT_TO_SPECIALIST_ID,
    SPECIALIST_REGISTRY,
    classify_file,
    normalize_result,
    resolve_specialist_id,
)
from virtual_self.archive.llm.specialists._core import SpecialistResult
from virtual_self.archive.llm.specialists.specialists_prompts import PROMPTS_BY_SPECIALIST_ID

FIXTURES = Path(__file__).parent / "fixtures" / "archive"

REQUIRED_KEYS = {
    "parse_strategy",
    "event_type_hints",
    "pii_risk",
    "should_parse",
    "confidence",
    "notes",
}


def _assignment(specialist_id: str) -> dict:
    return {"specialist_id": specialist_id, "reason": "test"}


def _assert_result_shape(out: dict) -> None:
    assert REQUIRED_KEYS <= set(out.keys())
    assert isinstance(out["event_type_hints"], list)
    assert out["pii_risk"] in {"low", "medium", "high"}
    assert isinstance(out["should_parse"], bool)
    assert 0.0 <= out["confidence"] <= 1.0


@pytest.mark.parametrize("specialist_id", sorted(SPECIALIST_REGISTRY.keys()))
def test_registry_has_prompt(specialist_id: str) -> None:
    assert specialist_id in PROMPTS_BY_SPECIALIST_ID


def test_product_to_specialist_covers_detect_keys() -> None:
    for product in (
        "mail",
        "my_activity",
        "chrome",
        "youtube",
        "location",
        "keep",
        "photos",
        "messages",
    ):
        assert product in PRODUCT_TO_SPECIALIST_ID
        assert PRODUCT_TO_SPECIALIST_ID[product] in SPECIALIST_REGISTRY


def test_normalize_clamps_invalid_llm_output() -> None:
    fallback = SpecialistResult(
        parse_strategy="json_records",
        event_type_hints=["browse"],
        pii_risk="low",
        should_parse=True,
        confidence=0.5,
        notes="fb",
    )
    out = normalize_result(
        {
            "parse_strategy": "not_a_strategy",
            "event_type_hints": "browse",
            "pii_risk": "extreme",
            "should_parse": "yes",
            "confidence": 99,
            "notes": "x" * 500,
        },
        fallback=fallback,
    )
    assert out["parse_strategy"] == "json_records"
    assert out["event_type_hints"] == ["browse"]
    assert out["pii_risk"] == "low"
    assert out["confidence"] == 1.0
    assert len(out["notes"]) <= 400


def test_gmail_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "Mail" / "All mail Including Spam and Trash.mbox"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("gmail_sample.mbox").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("gmail"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert out["parse_strategy"] == "mbox_stream"
    assert "email" in out["event_type_hints"][0]


def test_activity_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "My Activity" / "YouTube" / "MyActivity.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("my_activity.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("activity"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert "activity" in out["event_type_hints"]


def test_chrome_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "Chrome" / "BrowserHistory.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("chrome_history.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("chrome"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert out["event_type_hints"] == ["browse"]


def test_youtube_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "YouTube and YouTube Music" / "history" / "watch-history.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("youtube_history.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("youtube"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert "watch" in out["event_type_hints"]


def test_location_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "Location History" / "Records.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("location_sample.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    assert resolve_specialist_id(detected) == "location"
    out = classify_file(_assignment("location"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert out["pii_risk"] == "high"


def test_keep_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "Keep" / "note.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("keep_note.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("keep"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert "note" in out["event_type_hints"]


def test_photos_meta_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "takeout"
    path = root / "Google Photos" / "metadata.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("photos_meta.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path)
    out = classify_file(_assignment("photos_meta"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert "photo_metadata" in out["event_type_hints"]


def test_meta_messages_heuristic(tmp_path: Path) -> None:
    root = tmp_path / "meta"
    path = root / "messages" / "inbox" / "thread" / "message_1.json"
    path.parent.mkdir(parents=True)
    sample = FIXTURES.joinpath("meta_message.json").read_text()
    path.write_text(sample)
    detected = detect_classify(root, path, default_provider="meta")
    out = classify_file(_assignment("meta_messages"), sample, detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is True
    assert out["event_type_hints"] == ["message"]


def test_unknown_heuristic() -> None:
    detected = {"rel_path": "misc/foo.bin", "provider": "google", "product": "unknown", "mime_hint": "octet"}
    out = classify_file(_assignment("unknown"), "binary\xff\xfe", detected, use_llm=False)
    _assert_result_shape(out)
    assert out["should_parse"] is False


def test_classify_merges_llm_with_heuristic_fallback() -> None:
    detected = {
        "rel_path": "Chrome/BrowserHistory.json",
        "provider": "google",
        "product": "chrome",
        "mime_hint": "json",
    }
    sample = FIXTURES.joinpath("chrome_history.json").read_text()
    llm_out = {
        "parse_strategy": "json_nested",
        "event_type_hints": ["browse"],
        "pii_risk": "medium",
        "should_parse": True,
        "confidence": 0.95,
        "notes": "ok",
    }
    with (
        patch("virtual_self.archive.llm.specialists._core.chat_json", return_value=llm_out),
        patch("virtual_self.archive.llm.specialists._core.get_settings") as gs,
    ):
        gs.return_value.archive_llm_enabled = True
        out = classify_file(_assignment("chrome"), sample, detected, use_llm=True)
    assert out["confidence"] == 0.95
    assert out["notes"] == "ok"
