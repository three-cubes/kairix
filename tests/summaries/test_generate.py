"""
Tests for kairix.knowledge.summaries.generate

Uses ``SummariesDeps(chat=...)`` for dependency injection — no monkey-patching needed.
"""

import pytest

from kairix.knowledge.summaries.generate import (
    SummariesDeps,
    _first_n_words,
    generate_l0,
    generate_summaries,
)

# ---------------------------------------------------------------------------
# generate_l0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_l0_returns_string():
    """generate_l0() makes one API call and returns the abstract string."""
    expected = "This doc covers Azure Key Vault setup and token rotation."

    result = generate_l0(
        path="docs/azure.md",
        content="Some content about Azure Key Vault.",
        api_key="test-key",
        endpoint="https://test.openai.azure.com",
        deps=SummariesDeps(chat=lambda msgs, max_tokens=150: expected),
    )

    assert result == expected


@pytest.mark.unit
def test_generate_l0_uses_first_800_words():
    """generate_l0() passes only the first 800 words to the API."""
    words = [f"word_{i}" for i in range(1200)]
    long_content = " ".join(words)

    captured_messages: list = []

    def capture_chat(messages, max_tokens=150):
        captured_messages.append(messages)
        return "abstract"

    generate_l0(
        path="docs/long.md",
        content=long_content,
        api_key="k",
        endpoint="https://ep",
        deps=SummariesDeps(chat=capture_chat),
    )

    assert captured_messages, "chat callable was not invoked"
    user_msg = captured_messages[0][1]["content"]
    # Should contain word_799 but NOT word_800
    assert "word_799" in user_msg
    assert "word_800" not in user_msg


# ---------------------------------------------------------------------------
# _first_n_words
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_n_words_under_limit():
    assert _first_n_words("hello world", 10) == "hello world"


@pytest.mark.unit
def test_first_n_words_over_limit():
    text = " ".join(f"w{i}" for i in range(100))
    result = _first_n_words(text, 10)
    assert len(result.split()) == 10


@pytest.mark.unit
def test_first_n_words_empty():
    assert _first_n_words("", 10) == ""


# ---------------------------------------------------------------------------
# generate_summaries (integration-level via injected fake chat)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generate_summaries_returns_list(tmp_path):
    """generate_summaries returns a list of SummaryResult entries."""
    # Create temp files for generate_summaries to read
    (tmp_path / "a.md").write_text("Hello world content about testing.")
    (tmp_path / "b.md").write_text("Another document about architecture.")

    result = generate_summaries(
        [str(tmp_path / "a.md"), str(tmp_path / "b.md")],
        api_key="k",
        endpoint="https://ep",
        sleep_ms=0,  # keep the test fast — sleep path is exercised elsewhere
        deps=SummariesDeps(chat=lambda msgs, max_tokens=150: "Summary text."),
    )

    assert isinstance(result, list)
    assert len(result) == 2
    # Sabotage-prove: assert the chat-injected content actually flowed through.
    # If deps wasn't honoured, the production _default_chat would have been
    # invoked and raised on missing Azure credentials — so this also locks
    # in that we never reach the production callable in the test path.
    assert all(r.l0 == "Summary text." for r in result)
