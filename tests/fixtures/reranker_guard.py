"""Fast-tier reranker guard: a self-contained pytest plugin (#493).

Registered from the root ``tests/conftest.py`` via ``pytest_plugins``. Kept
free of kairix / tests imports so ``tests/test_fast_tier_reranker_guard.py``
can install this exact source as the conftest of a throwaway pytest run and
prove the hook's behaviour end to end.

The production cross-encoder reranker's first call imports torch and fetches
``cross-encoder/ms-marco-MiniLM-L-6-v2`` from the Hugging Face hub into the
session's hermetic (empty) cache: a real network call costing ~20-50s, which
tripped the 30s per-test timeout on whichever fast-tier test happened to
search first (the entity-summary / search-logging flake family). This guard
fails the fast-tier test that loaded the real stack, so the cost is attributed
deterministically to its cause instead of timing out a later bystander.
"""

from __future__ import annotations

import sys

import pytest

# Fast-tier markers (CI Stage 2: ``-m "unit or bdd or contract"``). Tests
# carrying any of these must never load the real cross-encoder stack.
FAST_TIER_MARKERS = frozenset({"unit", "bdd", "contract"})

GUARD_MESSAGE_PREFIX = "Real cross-encoder reranker load found in fast-tier test"


def real_sentence_transformers_loaded() -> bool:
    """True iff the real ``sentence_transformers`` package is in ``sys.modules``.

    ``kairix.core.search.rerank`` is the only importer of the package, so a
    real (on-disk, ``__file__``-carrying) module there means the production
    cross-encoder load ran. ``tests/search/test_rerank.py`` injects stub
    modules (no ``__file__``) and is unaffected.
    """
    module = sys.modules.get("sentence_transformers")
    return module is not None and getattr(module, "__file__", None) is not None


def _fail_if_loaded_by(item: pytest.Item, loaded_before: bool, original: BaseException | None = None) -> None:
    """Fail ``item`` when it is fast-tier and loaded the real module during its call."""
    fast_tier = any(item.get_closest_marker(name) for name in FAST_TIER_MARKERS)
    if not fast_tier or loaded_before or not real_sentence_transformers_loaded():
        return
    original_note = ""
    if original is not None:
        original_note = f"\nOriginal error raised by the test call: {type(original).__name__}: {original}"
    pytest.fail(
        f"{GUARD_MESSAGE_PREFIX} {item.nodeid} "
        "(imports torch + downloads a Hugging Face model over the network). "
        "Refactor to build the pipeline with "
        "FactoryDeps(reranker_override=RERANK_DISABLED) (or a fake reranker "
        "closure) to pass.\n"
        "Pass: build_search_pipeline(config=cfg, paths=paths, "
        "deps=FactoryDeps(reranker_override=RERANK_DISABLED))\n"
        "Forbidden: build_search_pipeline(config=cfg, paths=paths)  "
        "# default deps wire the real cross-encoder" + original_note,
        pytrace=False,
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Fail any fast-tier test that loads the real cross-encoder reranker.

    The check runs whether the call returns OR raises — including a
    pytest-timeout interruption mid ``CrossEncoder(...)`` load. Without that,
    a raising call would leave the real module loaded unattributed and every
    later test would see ``loaded_before=True`` and skip the guard,
    recreating the bystander failure.

    Which error wins: when the call raised AND loaded the module, the guard
    failure is reported (it names the root cause), chained to the original
    exception as its ``__context__``, and the original type + message is appended to the
    guard message so it stays visible in the ``-q`` summary. When the call
    raised without loading the module, the original exception propagates
    unchanged. ``KeyboardInterrupt`` / ``SystemExit`` always propagate
    unchanged so an operator abort is never masked.
    """
    loaded_before = real_sentence_transformers_loaded()
    try:
        result = yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        _fail_if_loaded_by(item, loaded_before, original=exc)
        raise
    _fail_if_loaded_by(item, loaded_before)
    return result
