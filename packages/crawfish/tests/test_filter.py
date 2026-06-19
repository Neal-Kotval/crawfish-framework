"""Tests for Filter — routing/narrowing a list Output (CRA-105)."""

from __future__ import annotations

from crawfish.nodes.filter import (
    Filter,
    field_equals,
    field_matches,
    limit,
    title_contains,
)
from crawfish.output import Output


def _items() -> list[dict[str, object]]:
    return [
        {"title": "a", "state": "open", "score": 1},
        {"title": "bill writer x", "state": "closed", "score": 2},
        {"title": "bill writer y", "state": "open", "score": 3},
        {"title": "c", "state": "open", "score": 4},
    ]


def _output() -> Output[list[dict[str, object]]]:
    return Output(output_schema=[], value=_items(), produced_by="src")


def test_title_contains_keeps_matches_in_order() -> None:
    out = _output()
    result = title_contains("bill writer").apply(out)
    assert [item["title"] for item in result.value] == ["bill writer x", "bill writer y"]


def test_input_unchanged_and_result_has_new_id() -> None:
    out = _output()
    original_value = out.value
    original_id = out.id

    result = title_contains("bill writer").apply(out)

    # Input Output is untouched (frozen): same list object, same id.
    assert out.value is original_value
    assert out.value == _items()
    assert out.id == original_id
    # The derived Output is a distinct envelope.
    assert result.id != original_id
    assert result.produced_by != "src"


def test_field_equals() -> None:
    result = field_equals("state", "open").apply(_output())
    assert [item["title"] for item in result.value] == ["a", "bill writer y", "c"]


def test_field_matches_regex_search() -> None:
    result = field_matches("title", r"^bill").apply(_output())
    assert [item["title"] for item in result.value] == ["bill writer x", "bill writer y"]


def test_field_matches_no_match_returns_empty() -> None:
    result = field_matches("title", r"zzz").apply(_output())
    assert result.value == []


def test_limit_keeps_first_n() -> None:
    result = limit(2).apply(_output())
    assert [item["title"] for item in result.value] == ["a", "bill writer x"]


def test_limit_larger_than_list_keeps_all() -> None:
    result = limit(99).apply(_output())
    assert len(result.value) == 4


def test_limit_is_a_filter() -> None:
    assert isinstance(limit(1), Filter)


def test_filters_compose() -> None:
    out = _output()
    step1 = field_equals("state", "open").apply(out)
    step2 = title_contains("bill writer").apply(step1)
    step3 = limit(1).apply(step2)

    assert [item["title"] for item in step2.value] == ["bill writer y"]
    assert [item["title"] for item in step3.value] == ["bill writer y"]
    # Original input remains intact through the whole chain.
    assert out.value == _items()


def test_custom_predicate() -> None:
    high = Filter(lambda item: item["score"] >= 3, name="high_score")
    result = high.apply(_output())
    assert [item["score"] for item in result.value] == [3, 4]
    assert high.name == "high_score"
    assert high.kind.value == "filter"
