"""F87: every persist/load pair ships an adversarial round-trip corpus.

Motivation (EPIC #499 Phase 1; the GitHub-PEM escape-class — session 2)
----------------------------------------------------------------------
A persist/load pair is a write function plus its read counterpart over
the SAME store — ``set_secret`` writes the operator bundle,
``load_secrets_file`` parses it back; ``FileTokenStore`` writes through
``set_secret`` and the secrets read path resolves it. These pairs are
where the wizard's GitHub leg died after consent: ``set_secret``
rejected multi-line values, so the GitHub App PEM private key (multi-line
by definition) could never be stored — the connection ALWAYS failed
after the operator had already approved it, and every persist-side test
passed because they only ever round-tripped a single-line token.

That class of bug — a value shape the happy-path test never exercises
slipping through a green suite — is exactly what F87 mechanises. Every
registered persist/load pair must ship a contract test that round-trips
ADVERSARIAL material across the write→read boundary, covering four
shape classes the happy path skips:

  * **multi-line** — embedded ``\\n`` / ``\\r`` (the PEM class).
  * **unicode** — emoji AND CJK code points (UTF-8 byte-width edges).
  * **large** — a value ≥ 64 KiB (buffer / line-length ceilings).
  * **escape-lookalike** — backslash sequences that look like escapes
    but must survive verbatim (``C:\\new\\path``, a literal ``\\n``).

The pair-registry (the contract)
--------------------------------
``_PAIRS`` is a DECLARED registry — ``pair_name -> (write_symbol,
read_symbol, marker)`` — not an auto-discovery sweep. Precision over
recall: F87 fires only on the pairs an engineer has registered, never on
every ``open()`` / ``write()`` in the tree. Adding a persist/load pair
to production is a deliberate act; registering it here is the matching
deliberate act, and the seam is one row.

The coverage convention (document of record)
--------------------------------------------
A registered pair ``P`` is corpus-covered when SOME test module under
``tests/`` satisfies BOTH:

  1. **Pair anchor** — the module carries a ``# F87-corpus: <pair_name>``
     marker line. The marker is the reviewed declaration that this
     module is ``P``'s adversarial round-trip corpus; it also names the
     pair so a reader greps straight to it. A module may anchor more
     than one pair (one marker line each).
  2. **Four material classes** — the module's text carries a greppable
     token for EACH of the four shape classes (multi-line, unicode,
     large, escape-lookalike). Tokens are matched by the per-class
     regexes in ``_CLASS_SIGNALS`` — a ``\\n`` in a string literal, an
     emoji/CJK code point, a ``>= 64 * 1024`` / ``65536`` size literal,
     a ``F87-escape-lookalike`` / backslash-doubled literal. A
     ``# F87-corpus-class: <class>`` override line counts a class
     explicitly for corpora that build a class indirectly (e.g. a 64 KiB
     value composed at runtime rather than written as a literal).

The two parts are independent: the marker says WHICH pair, the class
signals prove the corpus is adversarial. A module missing either does
not cover the pair.

Pass example: ``tests/integration/test_secrets_pem_round_trip.py`` (the
GitHub-PEM fix) + ``tests/unit/test_secrets_encoding.py`` carry
``# F87-corpus: secrets_set_load`` and exercise the four classes across
``set_secret`` → ``load_secrets_file`` (and the
``encode_bundle_value`` / ``decode_bundle_value`` codec underneath) —
together they cover the secrets pair.

Intentionally NOT caught (precision over recall — a detector engineers
distrust is worse than no detector):

  * Persist/load pairs NOT in ``_PAIRS``. F87 is registry-scoped by
    design; it never auto-discovers a write/read boundary. A new pair
    ships uncovered until someone adds its row — that addition is the
    review gate, and an unregistered pair is invisible to F87 (the
    accepted under-catch).
  * WHETHER the four class signals occur in the SAME test FUNCTION as
    the round-trip, or whether the round-trip actually asserts equality.
    Detection is module-granular and signal-based; review (and the
    sabotage-proof every corpus test carries) holds the line on sincere
    coverage. A module that greps the four tokens but asserts nothing is
    review's catch, not the regex's.
  * The semantic correctness of the codec — F87 proves a corpus EXISTS
    and spans the four classes, not that the round-trip is bit-exact
    (that is the test body's job).
  * Non-registered stores that happen to share a symbol name with a
    registered pair — the registry keys on pair_name, and the marker
    names the pair, so a same-named symbol elsewhere is not conflated.

There is no grandfathering: every registered pair without a corpus
blocks at pre-commit / safe-commit / CI Stage 0.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate


@dataclass(frozen=True)
class PersistLoadPair:
    """One registered persist/load pair.

    ``write_symbol`` / ``read_symbol`` are the public write + read
    function (or class) names the corpus must reference. ``violation_path``
    is the repo-relative pseudo-path used to report an uncovered pair: F87
    has no source file to point at (the violation is a MISSING test), so
    each pair carries a stable synthetic path.
    """

    write_symbol: str
    read_symbol: str
    violation_path: str


# The DECLARED pair-registry — extend with one row per persist/load pair.
# Keyed by pair_name (the token the `# F87-corpus:` marker names).
_PAIRS: dict[str, PersistLoadPair] = {
    # set_secret writes the operator bundle; load_secrets_file parses it
    # back. The newline-safe codec (encode/decode_bundle_value) is the
    # escape layer the GitHub-PEM fix added — its symbols also count as
    # the write/read references for this pair.
    "secrets_set_load": PersistLoadPair(
        write_symbol="set_secret",
        read_symbol="load_secrets_file",
        violation_path="kairix/secrets/store.py::set_secret+load_secrets_file",
    ),
    # FileTokenStore.store persists captured connector tokens through the
    # secrets writer; the secrets read path resolves them on next boot.
    "connect_file_store": PersistLoadPair(
        write_symbol="FileTokenStore",
        read_symbol="load_secrets_file",
        violation_path="kairix/connect/store/file_store.py::FileTokenStore.store+load_secrets_file",
    ),
    # The wizard config writer; the canonical layered reader reads back.
    "config_write_load": PersistLoadPair(
        write_symbol="write_config_updates",
        read_symbol="load_merged_mapping",
        violation_path="kairix/platform/setup/backends.py::write_config_updates+load_merged_mapping",
    ),
    # EmbeddingCache persists vectors and reads them back over SQLite.
    "embedding_cache_put_get": PersistLoadPair(
        write_symbol="put_many",
        read_symbol="get_many",
        violation_path="kairix/core/embed/embedding_cache.py::put_many+get_many",
    ),
}

# Per-pair `# F87-corpus: <pair_name>` anchor.
_CORPUS_MARKER_RE = re.compile(r"#\s*F87-corpus:\s*([A-Za-z0-9_]+)")
# Per-class explicit override `# F87-corpus-class: <class>`.
_CLASS_OVERRIDE_RE = re.compile(r"#\s*F87-corpus-class:\s*([a-z_-]+)")

# Greppable signals for each adversarial material class. A module that
# anchors a pair must carry a token for EVERY class (or a class-override
# comment naming it).
_CLASS_SIGNALS: dict[str, re.Pattern[str]] = {
    # An embedded newline / carriage-return inside a string literal.
    "multi-line": re.compile(r'(?<!#)["\'].*\\[nr]', re.DOTALL),
    # An emoji or CJK-range code point in the source.
    "unicode": re.compile(
        "["
        "\U0001f000-\U0001ffff"  # emoji / symbols-and-pictographs planes
        "　-〿"  # CJK punctuation
        "㐀-䶿"  # CJK ext-A
        "一-鿿"  # CJK unified ideographs
        "가-힯"  # Hangul (CJK-family)
        "]"
    ),
    # A >= 64 KiB size literal: 65536, 65_536, 64*1024, 64 * 1024, 0x10000.
    "large": re.compile(r"\b65_?536\b|\b64\s*\*\s*1024\b|\b1024\s*\*\s*64\b|0x1_?0000\b|\b2\s*\*\*\s*16\b"),
    # A backslash-escape lookalike that must survive verbatim — a doubled
    # backslash in a literal, or the explicit class token.
    "escape-lookalike": re.compile(r"\\\\|escape-lookalike|F87-escape"),
}

REMEDIATION = """F87: a registered persist/load pair has no adversarial round-trip
corpus — the GitHub-PEM class where set_secret rejected multi-line
values, so the wizard's GitHub leg ALWAYS failed after consent while
every single-line-token test stayed green.

fix: add (or extend) a contract test module under tests/ that round-trips
the pair's write→read boundary across all four adversarial material
classes. The module must:
  1. carry a `# F87-corpus: <pair_name>` marker line naming the pair
     (the reviewed declaration that this module is the pair's corpus);
  2. reference the pair's write and read symbols; and
  3. exercise FOUR material classes — multi-line (embedded \\n/\\r),
     unicode (emoji AND CJK), large (a value >= 64 KiB), and
     escape-lookalike (backslash sequences that must survive verbatim).
Each class needs a greppable signal: a \\n in a literal, an emoji/CJK
code point, a 64*1024 / 65536 size literal, a doubled-backslash literal —
or a `# F87-corpus-class: <class>` override line when the class is built
indirectly (e.g. a 64 KiB value composed at runtime).
next: re-run python3 scripts/checks/check_f87_persist_load_corpus.py to
confirm the gate goes green. See tests/unit/test_secrets_encoding.py +
tests/integration/test_secrets_pem_round_trip.py for the canonical
corpus shape.
run: bash scripts/safe-commit.sh "test(<store>): adversarial round-trip corpus for <pair> (#499 phase 1)"

Pass example: tests/integration/test_secrets_pem_round_trip.py
  # F87-corpus: secrets_set_load
  from kairix.secrets import load_secrets_file
  from kairix.secrets.store import set_secret

  @pytest.mark.parametrize("value", [
      "line-one\\nline-two",          # multi-line
      "key-🔑-世界-value",             # unicode (emoji + CJK)
      "X" * (64 * 1024),              # large >= 64 KiB
      "C:\\\\new\\\\path",            # escape-lookalike
  ])
  def test_round_trip_byte_identical(value, tmp_path):
      bundle = tmp_path / "kairix.env"
      set_secret(_NAME, value, bundle_path=bundle)
      load_secrets_file.cache_clear()
      assert load_secrets_file(bundle)[_ENV] == value  # write -> read is lossless

Forbidden example:
  # tests/unit/test_secret_token.py — happy-path only:
  def test_token_round_trips(tmp_path):
      set_secret(_NAME, "plain-token-123", bundle_path=bundle)  # single line
      assert load_secrets_file(bundle)[_ENV] == "plain-token-123"
  # multi-line / unicode / 64KiB / escape values never exercised — the
  # exact gap that shipped the GitHub-PEM consent failure."""


def _test_files(repo_root: Path) -> list[Path]:
    """Every ``.py`` test module under ``tests/``, skipping ``__pycache__``."""
    tests_dir = repo_root / "tests"
    if not tests_dir.exists():
        return []
    return [p for p in sorted(tests_dir.rglob("*.py")) if "__pycache__" not in p.parts]


def _module_covers(text: str, pair: PersistLoadPair) -> bool:
    """True iff ``text`` references both pair symbols and carries a token
    for every adversarial material class."""
    if pair.write_symbol not in text or pair.read_symbol not in text:
        return False
    overrides = {m.group(1) for m in _CLASS_OVERRIDE_RE.finditer(text)}
    for class_name, signal in _CLASS_SIGNALS.items():
        if class_name in overrides:
            continue
        if not signal.search(text):
            return False
    return True


def _covered_pairs(repo_root: Path) -> set[str]:
    """Pair names whose corpus exists — a marked module referencing both
    symbols and spanning the four classes."""
    covered: set[str] = set()
    for path in _test_files(repo_root):
        text = path.read_text(encoding="utf-8")
        anchored = {m.group(1) for m in _CORPUS_MARKER_RE.finditer(text)}
        for pair_name in anchored & set(_PAIRS):
            if _module_covers(text, _PAIRS[pair_name]):
                covered.add(pair_name)
    return covered


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Resolve corpus coverage for every registered pair; print per-pair
    detail lines; return the synthetic violation paths of uncovered pairs."""
    covered = _covered_pairs(repo_root)
    violations: set[Path] = set()
    for pair_name in sorted(_PAIRS):
        if pair_name in covered:
            continue
        pair = _PAIRS[pair_name]
        violations.add(Path(pair.violation_path))
        print(
            f"  [f87] {pair.violation_path}: persist/load pair '{pair_name}' "
            f"({pair.write_symbol} -> {pair.read_symbol}) has no adversarial round-trip corpus"
        )
    return violations


def main() -> int:
    return gate("f87", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
