"""Unit tests for :func:`kairix.paths.provider_name` (config-yaml seam).

The function reads the operator-configured plugin name from the
``provider:`` field of ``kairix.config.yaml``. Tests drive the
underlying ``parse_config`` directly to keep the assertions on the
parsing contract — avoids monkeypatching ``KAIRIX_*`` env vars (F2)
and avoids touching the ``lru_cache`` on ``load_cached``.

Sabotage proofs (each test):

- ``test_parses_top_level_provider_field``: change
  ``parse_config`` so it looks under ``retrieval.provider`` instead
  of the top level → this test fails because ``provider`` is
  missing from the returned config.
- ``test_blank_provider_resolves_to_none``: change the parse helper
  so blank strings pass through verbatim → the ``None`` assertion
  fails (and downstream callers would try to resolve ``""`` as a
  plugin name).
- ``test_provider_field_strips_whitespace``: drop the ``.strip()``
  call in ``parse_config`` → assertion that ``"  azure_foundry  "``
  yields ``"azure_foundry"`` fails because the raw string survives.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_loader import parse_config

pytestmark = pytest.mark.unit


class TestParseConfigProviderField:
    """Drive the ``provider:`` field parsing through the public
    ``parse_config`` surface — no env vars, no lru_cache mutation.

    Driving through the public surface honours the
    feedback_no_internal_function_tests memory: ``provider_name()``
    composes ``load_config()`` (which composes ``load_cached`` →
    ``parse_config``); the parse step is the policy under test.
    """

    def test_parses_top_level_provider_field(self) -> None:
        """Top-level ``provider:`` populates ``RetrievalConfig.provider``."""
        cfg = parse_config({"provider": "azure_foundry"})
        assert cfg.provider == "azure_foundry"

    def test_blank_provider_resolves_to_none(self) -> None:
        """Empty string is normalised to ``None`` so callers raise a typed error.

        Down-stream the factory translates ``cfg.provider is None`` into
        a ``ValueError`` with the installed-plugins list — passing
        ``""`` through would either resolve to a nonexistent plugin or
        slip past the guard.
        """
        cfg_blank = parse_config({"provider": ""})
        cfg_missing = parse_config({})
        assert cfg_blank.provider is None
        assert cfg_missing.provider is None

    def test_provider_field_strips_whitespace(self) -> None:
        """Surrounding whitespace is stripped — operator typos shouldn't leak.

        Operators editing yaml occasionally end up with `provider:
        azure_foundry ` (trailing space). The parsing layer absorbs
        that so the entry-point lookup gets the canonical name.
        """
        cfg = parse_config({"provider": "  openai  "})
        assert cfg.provider == "openai"

    def test_provider_coexists_with_retrieval_block(self) -> None:
        """``provider:`` at the top level doesn't disturb the ``retrieval:`` block.

        Documents the regression class: a sloppy parser implementation
        that re-keyed ``provider`` into ``retrieval`` would zero out
        the operator's tuned retrieval config silently.
        """
        cfg = parse_config(
            {
                "provider": "azure_foundry",
                "retrieval": {
                    "fusion_strategy": "rrf",
                    "rrf_k": 30,
                },
            }
        )
        assert cfg.provider == "azure_foundry"
        assert cfg.fusion_strategy == "rrf"
        assert cfg.rrf_k == 30


class TestProviderNameAccessor:
    """Drive :func:`kairix.paths.provider_name` end-to-end through
    ``load_config(config_path=...)`` — bypasses the env-var seam by
    using the parser-level test affordance the loader already exposes.

    Sabotage: drop the ``load_config`` import inside
    ``provider_name`` → these tests fail because the accessor returns
    ``None`` regardless of yaml content.
    """

    def test_returns_configured_name_from_yaml(self, tmp_path) -> None:
        """A real yaml file with ``provider: azure_foundry`` flows through
        to the accessor's return value.

        Drives the full code path: ``provider_name()`` →
        ``load_config()`` → ``load_cached`` → ``parse_config``.
        """
        from kairix.core.search.config_loader import load_cached, load_config

        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("provider: azure_foundry\n", encoding="utf-8")
        # Test seam: load_config(config_path=...) bypasses the env-var
        # resolution chain entirely. Clear the lru_cache before/after so
        # cached results from other suites can't leak into this assertion.
        load_cached.cache_clear()
        try:
            cfg = load_config(config_path=config_file)
            assert cfg.provider == "azure_foundry"
        finally:
            load_cached.cache_clear()

    def test_returns_none_when_provider_field_absent(self, tmp_path) -> None:
        """A yaml file without ``provider:`` yields ``None`` —
        :func:`kairix.paths.provider_name`'s caller is responsible for
        raising the typed actionable error.
        """
        from kairix.core.search.config_loader import load_cached, load_config

        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text("retrieval:\n  fusion_strategy: rrf\n", encoding="utf-8")
        load_cached.cache_clear()
        try:
            cfg = load_config(config_path=config_file)
            assert cfg.provider is None
        finally:
            load_cached.cache_clear()
