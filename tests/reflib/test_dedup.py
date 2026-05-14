"""Tests for reference library deduplication."""

from pathlib import Path

import pytest

from kairix.knowledge.reflib.dedup import (
    choose_canonical,
    find_exact_duplicates,
    find_near_duplicates,
    hash_content,
    jaccard_similarity,
)


@pytest.mark.unit
class TestHashContent:
    @pytest.mark.unit
    def test_same_content_same_hash(self):
        assert hash_content("Hello world") == hash_content("Hello world")

    @pytest.mark.unit
    def test_different_content_different_hash(self):
        assert hash_content("Hello") != hash_content("World")


@pytest.mark.unit
class TestJaccardSimilarity:
    @pytest.mark.unit
    def test_identical_texts(self):
        assert jaccard_similarity("the quick brown fox", "the quick brown fox") == pytest.approx(1.0)

    @pytest.mark.unit
    def test_similar_texts(self):
        sim = jaccard_similarity("the quick brown fox", "the quick brown dog")
        assert 0.5 < sim < 1.0

    @pytest.mark.unit
    def test_completely_different(self):
        sim = jaccard_similarity("aaa bbb ccc", "xxx yyy zzz")
        assert sim < 0.1

    @pytest.mark.unit
    def test_empty_text(self):
        # Empty strings produce empty shingle sets
        assert jaccard_similarity("", "abc def ghi") == pytest.approx(0.0)


@pytest.mark.unit
class TestFindExactDuplicates:
    @pytest.mark.unit
    def test_finds_duplicates(self):
        files = [
            (Path("a/doc.md"), "hash1"),
            (Path("b/doc.md"), "hash1"),
            (Path("c/other.md"), "hash2"),
        ]
        dupes = find_exact_duplicates(files)
        assert "hash1" in dupes
        assert len(dupes["hash1"]) == 2
        assert "hash2" not in dupes

    @pytest.mark.unit
    def test_no_duplicates(self):
        files = [
            (Path("a.md"), "hash1"),
            (Path("b.md"), "hash2"),
        ]
        assert find_exact_duplicates(files) == {}


@pytest.mark.unit
class TestChooseCanonical:
    @pytest.mark.unit
    def test_prefers_longer_body(self):
        paths = [Path("short.md"), Path("long.md")]
        bodies = {Path("short.md"): "x" * 10, Path("long.md"): "x" * 1000}
        result = choose_canonical(paths, bodies)
        assert result == Path("long.md")

    @pytest.mark.unit
    def test_single_path(self):
        assert choose_canonical([Path("only.md")]) == Path("only.md")

    @pytest.mark.unit
    def test_empty_raises(self):
        with pytest.raises(ValueError):
            choose_canonical([])


@pytest.mark.unit
class TestJaccardEmptyShingles:
    """Cover the empty-shingle short-circuit at line 31."""

    @pytest.mark.unit
    def test_both_empty(self):
        # Two empty strings → both produce {""}; intersection={""}, union={""}
        # so similarity is 1.0 (degenerate but defined).
        assert jaccard_similarity("", "") == pytest.approx(1.0)


@pytest.mark.unit
class TestFindNearDuplicates:
    @pytest.mark.unit
    def test_same_stem_near_duplicates_detected(self):
        """Files with same stem and high similarity surface as near-duplicates."""
        files = [
            (Path("collection_a/guide.md"), "The quick brown fox jumps over the lazy dog. " * 5),
            # Slight variation — same stem
            (Path("collection_b/guide.md"), "The quick brown fox jumps over the lazy dog! " * 5),
        ]
        duplicates = find_near_duplicates(files, threshold=0.8)
        assert len(duplicates) == 1
        path_a, path_b, sim = duplicates[0]
        assert sim >= 0.8
        # Either ordering is fine
        assert {path_a, path_b} == {Path("collection_a/guide.md"), Path("collection_b/guide.md")}

    @pytest.mark.unit
    def test_different_stem_skipped(self):
        """Files with different stems are not compared even when similar."""
        text = "The quick brown fox jumps over the lazy dog. " * 5
        files = [
            (Path("a/guide.md"), text),
            (Path("b/manual.md"), text),
        ]
        duplicates = find_near_duplicates(files, threshold=0.8)
        assert duplicates == []

    @pytest.mark.unit
    def test_single_file_per_stem_no_duplicates(self):
        """When only one file shares a stem, the inner loop is skipped."""
        files = [
            (Path("a/guide.md"), "content a"),
            (Path("b/other.md"), "content b"),
        ]
        assert find_near_duplicates(files) == []

    @pytest.mark.unit
    def test_below_threshold_excluded(self):
        """Files with same stem but low similarity are not flagged."""
        files = [
            (Path("a/guide.md"), "aaa bbb ccc " * 10),
            (Path("b/guide.md"), "xxx yyy zzz " * 10),
        ]
        duplicates = find_near_duplicates(files, threshold=0.9)
        assert duplicates == []

    @pytest.mark.unit
    def test_threshold_parameter_respected(self):
        """Lower threshold catches more pairs."""
        files = [
            (Path("a/guide.md"), "alpha beta gamma " * 5),
            (Path("b/guide.md"), "alpha beta delta " * 5),
        ]
        loose = find_near_duplicates(files, threshold=0.3)
        strict = find_near_duplicates(files, threshold=0.95)
        assert len(loose) >= len(strict)
