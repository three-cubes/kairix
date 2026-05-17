"""Unit tests for :func:`kairix.quality.probe.config_cli.main`.

Drives the argparse + JSON-emit surface of ``kairix probe-config`` —
the operator-facing CLI introduced by #provider-plugin-arch IM-9.
The CLI:

- resolves a provider name via ``--provider`` or the injected
  ``env_provider_lookup`` callable (defaults to
  :func:`kairix.paths.provider_name`),
- runs :func:`run_probe_config` against the resolved provider,
- optionally diffs against a baseline JSON report (``--compare``),
- emits the JSON report to stdout or ``--output`` path,
- returns the report's ``exit_code`` (0 / 1 / 2) — argparse-style 2
  for usage errors.

Test seam:

- ``registry`` kwarg of :func:`main` accepts a
  :class:`tests.fakes.FakeProviderRegistry` so no entry-point
  discovery is required.
- ``snapshotter`` kwarg accepts a stub returning a fixed
  ``TransportSnapshot`` so the runner's transport-stats branch is
  exercised without touching real transport modules.
- ``env_provider_lookup`` kwarg accepts a callable returning the
  desired provider name (or ``None``), avoiding env-var mutation
  entirely.

Every test marks ``@pytest.mark.unit`` (F8) and embeds a sabotage-
proof note.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.quality.probe.config_cli import main
from kairix.quality.probe.config_runner import TransportSnapshot
from tests.fakes import FakeProvider, FakeProviderRegistry


class _StubSnapshotter:
    """Returns a fixed ``TransportSnapshot`` so the runner doesn't poke real transport."""

    def snapshot(self) -> TransportSnapshot:
        return TransportSnapshot(
            coalesce_ratio=0.1,
            cache_hit_rate=0.5,
            pool_acquire_p50_ms=5.0,
        )


def _registry_with(name: str = "openai") -> FakeProviderRegistry:
    """Build a registry mapping ``name`` → ``FakeProvider``."""
    return FakeProviderRegistry({name: FakeProvider(name=name, vector=[0.1, 0.2, 0.3])})


def _short_argv(*extra: str) -> list[str]:
    """Build a fast-running argv slice with the smallest legal sample counts."""
    return ["--warm-samples", "1", "--concurrency", "1", "--repeated-samples", "1", *extra]


# ---------------------------------------------------------------------------
# Happy path — emits a JSON report to stdout and returns the report's exit code
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_emits_json_report_and_returns_report_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A configured provider produces a JSON report on stdout and exit code 0.

    Sabotage-proof: removing the ``_emit_report(report, args.output)``
    call from main() leaves stdout empty; ``json.loads(captured.out)``
    raises ``JSONDecodeError`` and the test fails before reaching the
    exit-code assertion.
    """
    rc = main(
        _short_argv("--provider", "openai"),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert payload["provider"]["name"] == "openai"
    assert payload["status"] in {"healthy", "degraded", "unreachable"}


@pytest.mark.unit
def test_main_uses_env_lookup_when_provider_flag_absent() -> None:
    """No ``--provider`` flag → env_provider_lookup callable supplies the name.

    Sabotage-proof: removing the env-lookup fallback in
    ``_resolve_provider_name`` makes the lookup return ``None`` and
    the CLI returns exit code 2 (usage error) instead of the report
    exit code.
    """
    rc = main(
        _short_argv(),
        registry=_registry_with("anthropic"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: "anthropic",
    )

    # FakeProvider's healthcheck reports ok=True by default, so the
    # health verdict is healthy (exit 0).
    assert rc == 0


# ---------------------------------------------------------------------------
# Usage-error branches (exit code 2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_returns_2_when_no_provider_configured(capsys: pytest.CaptureFixture[str]) -> None:
    """Neither flag nor env supplies a provider → exit code 2 + stderr affordance.

    Sabotage-proof: removing the ``if not name: return None,
    _invalid_args(...)`` guard makes get_provider() crash on the
    ``None`` name and the rc isn't 2.
    """
    rc = main(
        _short_argv(),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "no provider configured" in captured.err
    assert "fix:" in captured.err


@pytest.mark.unit
def test_main_returns_2_when_provider_not_registered(capsys: pytest.CaptureFixture[str]) -> None:
    """Unknown provider name → ProviderNotRegistered → exit code 2.

    Sabotage-proof: removing the ``except ProviderNotRegistered`` in
    ``_resolve_provider`` propagates the exception; the test sees an
    uncaught exception instead of rc=2.
    """
    rc = main(
        _short_argv("--provider", "no_such_plugin"),
        registry=_registry_with("openai"),  # only 'openai' is registered
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    # The ProviderNotRegistered.__str__ surfaces in the actionable error.
    captured = capsys.readouterr()
    assert "no_such_plugin" in captured.err
    assert "fix:" in captured.err


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (["--warm-samples", "0", "--concurrency", "1", "--repeated-samples", "1"], "warm-samples"),
        (["--warm-samples", "1", "--concurrency", "0", "--repeated-samples", "1"], "concurrency"),
        (["--warm-samples", "1", "--concurrency", "1", "--repeated-samples", "0"], "repeated-samples"),
    ],
)
def test_main_returns_2_when_sample_flag_below_minimum(
    capsys: pytest.CaptureFixture[str], argv: list[str], needle: str
) -> None:
    """Sample-count flags below 1 → exit code 2 + actionable stderr.

    Sabotage-proof: dropping any of the three ``if args.X < 1: return
    _invalid_args(...)`` guards lets the runner attempt N=0 samples
    and either hangs or surfaces a different error class.
    """
    rc = main(
        [*argv, "--provider", "openai"],
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert needle in captured.err


# ---------------------------------------------------------------------------
# --output writes the report to a file instead of stdout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_writes_report_to_output_path_when_supplied(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--output report.json`` writes JSON to file and emits nothing on stdout.

    Sabotage-proof: removing the ``output_path`` branch in
    ``_emit_report`` makes the function always print to stdout; the
    file would be empty and the JSON assertion fails.
    """
    out_path = tmp_path / "report.json"
    rc = main(
        _short_argv("--provider", "openai", "--output", str(out_path)),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["provider"]["name"] == "openai"


# ---------------------------------------------------------------------------
# --compare baseline JSON drives the comparison branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_attaches_comparison_when_baseline_supplied(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid baseline path populates ``report.comparison``.

    Sabotage-proof: removing the ``report = _attach_comparison(...)``
    line in main() leaves the report's ``comparison`` field as None;
    the assert on a populated comparison section fails.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "stage_latency_ms": {"cold": 100.0, "warm_sequential": 50.0},
                "collected_at": "2024-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        _short_argv("--provider", "openai", "--compare", str(baseline)),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 0
    assert payload.get("comparison") is not None


@pytest.mark.unit
def test_main_returns_2_when_baseline_path_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A nonexistent baseline path → exit code 2 + JSON still emitted.

    Sabotage-proof: removing the existence check makes the bare
    ``open()`` raise FileNotFoundError which doesn't pattern-match
    the rc=2 contract.
    """
    missing = tmp_path / "absent.json"

    rc = main(
        _short_argv("--provider", "openai", "--compare", str(missing)),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    captured = capsys.readouterr()
    # Per the implementation note, the report is still emitted on
    # stdout so the operator can see the verdict.
    assert captured.out.strip() != ""
    assert "does not exist" in captured.err


@pytest.mark.unit
def test_main_returns_2_when_baseline_is_malformed_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed baseline JSON → exit code 2 + JSON still emitted.

    Sabotage-proof: removing the ``except (OSError, json.JSONDecodeError)``
    block lets the parse error propagate; the rc=2 contract is broken.
    """
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")

    rc = main(
        _short_argv("--provider", "openai", "--compare", str(bad)),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err


@pytest.mark.unit
def test_main_handles_baseline_with_non_dict_stage_latency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A baseline whose ``stage_latency_ms`` isn't a dict short-circuits to {}.

    Sabotage-proof: dropping the ``if not isinstance(..., dict):
    baseline_stages = {}`` guard in ``_attach_comparison`` would
    surface AttributeError on the ``.items()`` call. The CLI would
    crash; the rc=0 contract fails.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "stage_latency_ms": "this is not a dict",
                "collected_at": "2024-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        _short_argv("--provider", "openai", "--compare", str(baseline)),
        registry=_registry_with("openai"),
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    # Comparison still emitted (with empty baseline stages → no regressions).
    assert payload.get("comparison") is not None


# ---------------------------------------------------------------------------
# Unreachable provider — exit code mirrors EXIT_CODE_UNREACHABLE
# ---------------------------------------------------------------------------


class _UnreachableProvider:
    """``Provider`` that always raises ProviderUnreachable.

    Used to drive the ``if report.exit_code == EXIT_CODE_UNREACHABLE``
    branch in ``main`` — when the underlying probe cannot reach the
    endpoint, the runner marks the status as unreachable and the CLI
    returns exit code 2.
    """

    name = "broken"

    def embed_batch(self, _texts: list[str]) -> list[list[float]]:
        from kairix.providers import ProviderUnreachable

        raise ProviderUnreachable("simulated network failure")

    def chat(self, _messages: list[dict[str, object]], *, max_tokens: int = 800) -> str:
        del max_tokens
        return ""

    def dimension(self) -> int:
        return 1536

    def healthcheck(self) -> object:
        from kairix.providers import ProviderHealth

        return ProviderHealth(ok=False, endpoint="broken", error="ProviderUnreachable")


@pytest.mark.unit
def test_main_returns_unreachable_exit_code_when_provider_is_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreachable provider surfaces exit code 2 via the EXIT_CODE_UNREACHABLE branch.

    Sabotage-proof: removing the ``if report.exit_code ==
    EXIT_CODE_UNREACHABLE: return EXIT_CODE_UNREACHABLE`` defensive
    branch in main() makes the CLI return the dataclass exit_code
    directly — which is also 2 today, but the explicit defensive
    branch is what the test pins. Mutating it to ``return 0`` makes
    this assertion fail.
    """
    registry = FakeProviderRegistry({"broken": _UnreachableProvider()})
    rc = main(
        _short_argv("--provider", "broken"),
        registry=registry,
        snapshotter=_StubSnapshotter(),
        env_provider_lookup=lambda: None,
    )

    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "unreachable"
