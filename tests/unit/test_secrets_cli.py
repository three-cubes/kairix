"""Outcome tests for ``kairix secrets verify`` and ``kairix secrets set``.

F30-compliant: asserts on stdout / stderr / exit-code envelope, not
just returncode. F2-clean: passes a :class:`FakeSecretsLoader` through
the CLI's ``loader_factory`` DI seam — no ``monkeypatch.setenv``; the
``set`` tests pass the target bundle through the ``bundle_path`` seam
(in-process) or an explicit ``env=`` dict (subprocess — the established
outcome-test pattern, see tests/integration/test_connect_google_lifecycle.py).
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from kairix.secrets.cli import main as secrets_main
from tests.fakes import FakeSecretsLoader

pytestmark = pytest.mark.unit


def _identities_for_test() -> tuple[tuple[str, str, str | None, str], ...]:
    """Tiny identity set so the verify table stays inspectable in tests."""
    return (
        ("connector", "m365", None, "tenant-id"),
        ("connector", "m365", None, "client-secret"),
        ("provider", "llm", None, "api-key"),
    )


def _capture(argv: list[str], **kwargs) -> tuple[str, int]:
    """Run secrets_main(argv, ...) and capture (stdout, exit_code)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = secrets_main(argv, **kwargs)
    return buf.getvalue(), rc if rc is not None else 0


# ── verify: all present ────────────────────────────────────────────


def test_verify_all_present_exits_zero() -> None:
    """Every identity resolves via the fake loader → table renders + rc=0."""
    fake = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "tenant-1",
            ("connector", "m365", None, "client-secret"): "secret-1",
            ("provider", "llm", None, "api-key"): "key-1",
        },
    )
    stdout, rc = _capture(
        ["verify"],
        loader_factory=lambda: fake,
        identities_provider=_identities_for_test,
    )
    assert rc == 0
    assert "present" in stdout
    assert "kairix-connector-m365-tenant-id" in stdout
    assert "kairix-provider-llm-api-key" in stdout
    assert "MISSING" not in stdout


def test_verify_missing_exits_nonzero_and_marks_row() -> None:
    """A missing secret marks the row MISSING and returns exit 1."""
    fake = FakeSecretsLoader(
        values={
            ("connector", "m365", None, "tenant-id"): "tenant-1",
            # client-secret + llm api-key absent
        },
    )
    stdout, rc = _capture(
        ["verify"],
        loader_factory=lambda: fake,
        identities_provider=_identities_for_test,
    )
    assert rc == 1
    assert "MISSING" in stdout
    # Both missing rows surfaced in the same table.
    assert stdout.count("MISSING") == 2


def test_verify_json_envelope_shape() -> None:
    """--json emits a parseable envelope with a 'secrets' list."""
    fake = FakeSecretsLoader(
        values={("connector", "m365", None, "tenant-id"): "tenant-1"},
    )
    stdout, rc = _capture(
        ["verify", "--json"],
        loader_factory=lambda: fake,
        identities_provider=lambda: (("connector", "m365", None, "tenant-id"),),
    )
    assert rc == 0
    parsed = json.loads(stdout)
    assert "secrets" in parsed
    assert isinstance(parsed["secrets"], list)
    assert parsed["secrets"][0]["status"] == "present"
    assert parsed["secrets"][0]["canonical_kv"] == "kairix-connector-m365-tenant-id"


# ── subprocess-driven outcome test (F30 strict shape) ─────────────


def test_secrets_verify_via_subprocess() -> None:
    """F30: invoke via subprocess to exercise the full dispatcher.

    Subprocess invocation prints the verify table for the registered
    identities. The default loader resolves against the empty live
    environment, so every row reports MISSING and the command exits 1
    — that's the deterministic envelope shape we assert on.
    """
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "verify", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # Exit code is 1 because no canonical env vars are set in the
    # subprocess — every registered identity comes back MISSING.
    assert result.returncode == 1, f"expected exit 1; got rc={result.returncode}\nstderr={result.stderr!r}"
    parsed = json.loads(result.stdout)
    assert "secrets" in parsed
    assert len(parsed["secrets"]) > 0
    assert any(r["canonical_kv"] == "kairix-connector-m365-tenant-id" for r in parsed["secrets"])


def test_verify_loads_bundle_file_before_reading_env(tmp_path) -> None:
    """Verify must hydrate the bundle file into os.environ first.

    Regression test for #360: previously the verify CLI walked
    SecretsLoader.get() over the registered identities, which reads
    os.environ. Connectors + providers running in production load
    the bundle file implicitly via kairix.secrets.get_secret(), but
    'kairix secrets verify' invoked from `docker exec` did not — so
    every bundle-only secret showed as MISSING despite working at
    runtime. Fix: _ensure_bundle_loaded() calls load_secrets() at the
    top of _run_verify.

    Sabotage target: removing _ensure_bundle_loaded() makes this test
    fail because the canonical env var won't be present (the bundle
    file has 'KAIRIX_PROVIDER_LLM_API_KEY=...' but env doesn't until
    hydration).

    F2-clean: bundle_path and the env mapping the bundle hydrates into are
    passed explicitly via the secrets_main kwarg seams (``environ=`` plus a
    ``loader_factory`` reading the same mapping) — no process-env write.
    """
    from kairix.secrets.loader import SecretsLoader

    bundle = tmp_path / "kairix.env"
    bundle.write_text(
        "KAIRIX_PROVIDER_LLM_API_KEY=hydrated-from-bundle\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    env: dict[str, str] = {}

    out, rc = _capture(
        ["verify", "--json"],
        identities_provider=lambda: (("provider", "llm", None, "api-key"),),
        bundle_path=bundle,
        environ=env,
        loader_factory=lambda: SecretsLoader(env=env),
    )
    assert rc == 0, f"expected exit 0 (bundle hydrates key); got rc={rc}, out={out}"
    payload = json.loads(out)
    row = next(r for r in payload["secrets"] if r["leaf"] == "api-key")
    assert row["status"] == "present", f"expected key resolved via bundle hydration; got {row}"
    assert env == {"KAIRIX_PROVIDER_LLM_API_KEY": "hydrated-from-bundle"}  # pragma: allowlist secret


def test_verify_rehydrates_even_after_process_bootstrap_latched(tmp_path) -> None:
    """``verify`` forces a fresh hydration: a process whose boot-time
    bootstrap already latched (an earlier bundle) still sees the bundle
    verify is pointed at.

    Sabotage proof (executed): ``force=True`` -> ``False`` in
    ``_ensure_bundle_loaded`` → the latched guard skips the load, the key
    is MISSING and this fails; restored.
    """
    from kairix.secrets.bootstrap import bootstrap_secrets, reset_for_tests
    from kairix.secrets.loader import SecretsLoader

    boot_bundle = tmp_path / "boot.env"
    boot_bundle.write_text("KAIRIX_UNRELATED_BOOT_KEY=x\n", encoding="utf-8")
    verify_bundle = tmp_path / "kairix.env"
    verify_bundle.write_text(
        "KAIRIX_PROVIDER_LLM_API_KEY=hydrated-from-bundle\n",  # pragma: allowlist secret
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    reset_for_tests()
    try:
        assert bootstrap_secrets(bundle_path=boot_bundle, env={}) == 1  # latches the guard
        out, rc = _capture(
            ["verify", "--json"],
            identities_provider=lambda: (("provider", "llm", None, "api-key"),),
            bundle_path=verify_bundle,
            environ=env,
            loader_factory=lambda: SecretsLoader(env=env),
        )
    finally:
        reset_for_tests()
    assert rc == 0, out
    assert env == {"KAIRIX_PROVIDER_LLM_API_KEY": "hydrated-from-bundle"}  # pragma: allowlist secret


# ── set: persistence verb ──────────────────────────────────────────

_SET_NAME = "kairix-provider-llm-api-key"
_SET_ENV_VAR = "KAIRIX_PROVIDER_LLM_API_KEY"
_SET_VALUE = "example-credential-value"  # pragma: allowlist secret — generic fixture, not a real key


def _capture_both(argv: list[str], **kwargs) -> tuple[str, str, int]:
    """Run secrets_main(argv, ...) and capture (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = secrets_main(argv, **kwargs)
    return out.getvalue(), err.getvalue(), rc if rc is not None else 0


def test_set_with_inline_value_reports_path_and_persists(tmp_path: Path) -> None:
    """``set --value`` upserts the canonical line and reports the
    destination with the next-step affordance."""
    bundle = tmp_path / "kairix.env"
    stdout, stderr, rc = _capture_both(
        ["set", _SET_NAME, "--value", _SET_VALUE],
        bundle_path=bundle,
    )
    assert rc == 0, f"stderr={stderr!r}"
    assert f"Stored {_SET_NAME} in {bundle} (0600)." in stdout
    assert "next: kairix secrets verify" in stdout
    assert f"{_SET_ENV_VAR}={_SET_VALUE}" in bundle.read_text(encoding="utf-8")


def test_set_reads_value_from_stdin_seam(tmp_path: Path) -> None:
    """Without --value, the value comes from stdin (the leak-safe default).
    A single trailing newline — what ``echo`` appends — is stripped."""
    bundle = tmp_path / "kairix.env"
    stdout, _stderr, rc = _capture_both(
        ["set", _SET_NAME],
        bundle_path=bundle,
        value_reader=lambda: f"{_SET_VALUE}\n",
    )
    assert rc == 0
    assert f"Stored {_SET_NAME}" in stdout
    assert f"{_SET_ENV_VAR}={_SET_VALUE}\n" in bundle.read_text(encoding="utf-8")


def test_set_json_envelope_shape(tmp_path: Path) -> None:
    """--json emits a parseable envelope naming the secret + path, never the value."""
    bundle = tmp_path / "kairix.env"
    stdout, _stderr, rc = _capture_both(
        ["set", _SET_NAME, "--value", _SET_VALUE, "--json"],
        bundle_path=bundle,
    )
    assert rc == 0
    envelope = json.loads(stdout)
    assert envelope["stored"] == _SET_NAME
    assert envelope["path"] == str(bundle)
    assert envelope["mode"] == "0600"
    assert envelope["next"] == "kairix secrets verify"
    assert _SET_VALUE not in stdout


def test_set_rejects_non_canonical_name_with_examples(tmp_path: Path) -> None:
    """A non-canonical name exits 2 with an F21 affordance on stderr that
    quotes two valid example names."""
    bundle = tmp_path / "kairix.env"
    stdout, stderr, rc = _capture_both(
        ["set", "MY_API_KEY", "--value", _SET_VALUE],
        bundle_path=bundle,
    )
    assert rc == 2
    assert "fix:" in stderr
    assert "kairix-provider-llm-api-key" in stderr
    assert "kairix-connector-github-pat" in stderr
    assert not bundle.exists()
    assert _SET_VALUE not in stdout + stderr


def test_set_rejects_missing_value(tmp_path: Path) -> None:
    """Empty stdin and no --value exits 2 with stdin guidance."""
    bundle = tmp_path / "kairix.env"
    _stdout, stderr, rc = _capture_both(
        ["set", _SET_NAME],
        bundle_path=bundle,
        value_reader=lambda: "",
    )
    assert rc == 2
    assert "stdin" in stderr
    assert not bundle.exists()


def test_set_never_echoes_the_secret_value(tmp_path: Path) -> None:
    """F15: the stored value appears in the bundle file and NOWHERE in
    the command's combined output."""
    bundle = tmp_path / "kairix.env"
    stdout, stderr, rc = _capture_both(
        ["set", _SET_NAME, "--value", _SET_VALUE],
        bundle_path=bundle,
    )
    assert rc == 0
    assert _SET_VALUE in bundle.read_text(encoding="utf-8")
    assert _SET_VALUE not in stdout
    assert _SET_VALUE not in stderr


def test_set_via_subprocess_stdin_persists_with_0600(tmp_path: Path) -> None:
    """F30: full-dispatcher subprocess outcome — stdin value, explicit
    ``env=`` carrying KAIRIX_SECRETS_FILE (established outcome-test
    pattern; never setenv in-process)."""
    import subprocess

    bundle = tmp_path / "kairix.env"
    env = dict(os.environ)
    env["KAIRIX_SECRETS_FILE"] = str(bundle)
    result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "set", _SET_NAME],
        input=_SET_VALUE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert f"Stored {_SET_NAME} in {bundle} (0600)." in result.stdout
    assert "next: kairix secrets verify" in result.stdout
    assert f"{_SET_ENV_VAR}={_SET_VALUE}" in bundle.read_text(encoding="utf-8")
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    # F15: value never appears in any output stream.
    assert _SET_VALUE not in result.stdout
    assert _SET_VALUE not in result.stderr


def test_set_then_verify_round_trip_via_subprocess(tmp_path: Path) -> None:
    """The persisted secret resolves through the loader on the NEXT
    command: ``secrets set`` then ``secrets verify --json`` against the
    same bundle reports the identity as present."""
    import subprocess

    bundle = tmp_path / "kairix.env"
    env = dict(os.environ)
    env["KAIRIX_SECRETS_FILE"] = str(bundle)
    env.pop(_SET_ENV_VAR, None)  # the bundle, not inherited env, must resolve it

    set_result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "set", _SET_NAME],
        input=_SET_VALUE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert set_result.returncode == 0, f"stderr={set_result.stderr!r}"

    verify_result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "verify", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    payload = json.loads(verify_result.stdout)
    row = next(r for r in payload["secrets"] if r["canonical_kv"] == _SET_NAME)
    assert row["status"] == "present", f"persisted secret did not resolve: {row}"


@pytest.mark.skipif(
    Path("/run/secrets").is_dir(),
    reason=(
        "container layout present: the shared bundle resolution legitimately "
        "prefers /run/secrets/kairix.env over the XDG fallback there, so the "
        "pip-install round-trip below cannot be exercised on this host. The "
        "XDG branch itself is unit-pinned in tests/unit/test_secrets_store.py."
    ),
)
def test_set_then_verify_round_trip_via_xdg_default_path(tmp_path: Path) -> None:
    """Pip-install loop-closer: with NO $KAIRIX_SECRETS_FILE override,
    ``secrets set`` writes to the XDG bundle and the very next
    ``secrets verify`` hydrates it back — the read side resolves through
    the same default path the write side used. Pins the
    load_secrets → resolve_bundle_path wiring (#473)."""
    import subprocess

    env = dict(os.environ)
    env.pop("KAIRIX_SECRETS_FILE", None)
    env.pop(_SET_ENV_VAR, None)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")

    set_result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "set", _SET_NAME],
        input=_SET_VALUE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert set_result.returncode == 0, f"stderr={set_result.stderr!r}"
    bundle = tmp_path / "xdg" / "kairix" / "secrets" / "kairix.env"
    assert bundle.exists(), f"set did not write to the XDG default: {set_result.stdout!r}"

    verify_result = subprocess.run(
        [sys.executable, "-m", "kairix.cli", "secrets", "verify", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    payload = json.loads(verify_result.stdout)
    row = next(r for r in payload["secrets"] if r["canonical_kv"] == _SET_NAME)
    assert row["status"] == "present", f"XDG bundle was not hydrated by the read side: {row}"


def test_verify_with_nonexistent_bundle_passes_through_to_loader(tmp_path) -> None:
    """When bundle_path points at a non-existent file, verify still runs.

    Covers the load_secrets "file absent → return 0" branch via the
    public secrets_main seam (no internal-name imports). The verify
    walk completes against env-only resolution; the test just asserts
    the path runs cleanly.
    """
    nonexistent = tmp_path / "no-such.env"
    fake = FakeSecretsLoader(values={("provider", "llm", None, "api-key"): "k"})
    out, rc = _capture(
        ["verify", "--json"],
        loader_factory=lambda: fake,
        identities_provider=lambda: (("provider", "llm", None, "api-key"),),
        bundle_path=nonexistent,
    )
    assert rc == 0
    assert "secrets" in out
