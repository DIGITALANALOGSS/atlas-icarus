import pytest

from app import conversation_vane as vane
from app.conversation_search import SearchError


def evidence(message):
    return {
        "message": message,
        "sources": [
            {"metadata": {
                "title": "First source",
                "url": "https://example.org/first",
            }},
            {"metadata": {
                "title": "Second source",
                "url": "https://example.org/second",
            }},
        ],
    }


def test_rejects_invented_markdown_destination():
    data = evidence(
        "Official page [Documentation]"
        "(https://example.org/invented) [1]."
    )
    with pytest.raises(SearchError, match="absent from returned sources"):
        vane._answer(data)


def test_rejects_real_url_attached_to_wrong_source_number():
    data = evidence(
        "[Documentation](https://example.org/second) [1]."
    )
    with pytest.raises(SearchError, match="does not match"):
        vane._answer(data)


def test_accepts_matching_markdown_destination_and_number():
    data = evidence(
        "[Documentation](https://example.org/first) [1]."
    )
    assert "https://example.org/first" in vane._answer(data)


def test_number_only_citations_still_work():
    assert "Supported answer [1]." in vane._answer(
        evidence("Supported answer [1].")
    )


def test_same_title_does_not_make_different_urls_equivalent():
    data = evidence(
        "[First source](https://example.org/different-thread) [1]."
    )
    with pytest.raises(SearchError, match="absent from returned sources"):
        vane._answer(data)
