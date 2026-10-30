from __future__ import annotations

from virtual_self.services.retrieval_queries import queries_for_retrieval


def test_empty_or_whitespace_returns_empty():
    assert queries_for_retrieval("") == []
    assert queries_for_retrieval("   \n\t  ") == []


def test_trimmed_original_is_first_variant():
    assert queries_for_retrieval("  hello there  ") == ["hello there"]


def test_strips_who_was_boilerplate():
    out = queries_for_retrieval("Who was Ada Lovelace?")
    assert out[0] == "Who was Ada Lovelace?"
    assert "Ada Lovelace" in out
    assert len(out) <= 4


def test_who_is_mixed_case_name_and_surname_token():
    out = queries_for_retrieval("Who is Jordan example")
    assert out[0] == "Who is Jordan example"
    assert "Jordan" in out
    assert any(v.casefold() == "example" for v in out)


def test_strips_tell_me_about_boilerplate():
    out = queries_for_retrieval("Tell me about Project Helios")
    assert out[0] == "Tell me about Project Helios"
    assert "Project Helios" in out


def test_quoted_strings_become_variants():
    out = queries_for_retrieval('What do you recall about "Summer 2019" trip?')
    assert out[0].startswith("What do you")
    assert "Summer 2019" in out


def test_capitalized_name_phrases():
    out = queries_for_retrieval("Anything on John Smith and Jane Doe?")
    assert "John Smith" in out
    assert "Jane Doe" in out


def test_dedupes_exact_strings():
    out = queries_for_retrieval("Tell me about ada lovelace")
    assert len(out) == len(set(out))


def test_max_four_variants():
    text = (
        'Tell me about "Alpha" and "Beta" and Charlie Delta and Echo Foxtrot '
        "and Golf Hotel"
    )
    out = queries_for_retrieval(text)
    assert len(out) <= 4
