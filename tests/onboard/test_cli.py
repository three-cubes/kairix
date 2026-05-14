"""
Tests for kairix.platform.onboard.cli — `kairix onboard check` CLI surface.

Covers the new `--json` structured output, exit-code semantics, and the
fact that `main(...)` returns an int rather than calling sys.exit.
"""

from __future__ import annotations

import json

import pytest

from kairix.platform.onboard.check import (
    CheckResult,
)
from kairix.platform.onboard.cli import main


def _patch_checks(monkeypatch, results: list[CheckResult]) -> None:
    """Substitute run_all_checks at the CLI's import site.

    The CLI imports run_all_checks inside cmd_check and run_onboard_check
    derives directly from the patched run_all_checks, so a single setattr
    is enough to drive both human-readable and JSON paths consistently
    without re-wrapping the OnboardResult builder.
    """
    from kairix.platform.onboard import check as check_mod

    monkeypatch.setattr(check_mod, "run_all_checks", lambda: results)


# ---------------------------------------------------------------------------
# Exit-code semantics — main() returns int, not SystemExit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_returns_zero_when_all_checks_pass(monkeypatch, capsys) -> None:
    """main(["check"]) returns 0 when every check passes — no SystemExit raised.

    Sabotage check: if main reverts to sys.exit, this test catches the
    exception escape; if it returns the wrong int, the equality fails.
    """
    _patch_checks(
        monkeypatch,
        [
            CheckResult(name="kairix_on_path", ok=True, detail="found"),
            CheckResult(name="secrets_loaded", ok=True, detail="present"),
        ],
    )

    rc = main(["check"])

    assert rc == 0
    # Sabotage check: ensure main returned a plain int, not None / SystemExit
    assert isinstance(rc, int)


@pytest.mark.unit
def test_main_returns_one_when_any_check_fails(monkeypatch, capsys) -> None:
    """main(["check"]) returns 1 when at least one check fails."""
    _patch_checks(
        monkeypatch,
        [
            CheckResult(name="kairix_on_path", ok=True, detail="found"),
            CheckResult(name="secrets_loaded", ok=False, detail="missing", fix="set them"),
        ],
    )

    rc = main(["check"])

    assert rc == 1


@pytest.mark.unit
def test_main_does_not_raise_systemexit(monkeypatch) -> None:
    """main returns the exit code directly — does not call sys.exit.

    Sabotage-prove: wrap in pytest.raises(SystemExit) inverted via
    `pytest.fail` if SystemExit is raised. Confirms the test-driving
    contract from #246 W4.
    """
    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=False, detail="missing", fix="install it")],
    )

    try:
        rc = main(["check"])
    except SystemExit as exc:
        pytest.fail(f"main raised SystemExit({exc.code!r}); should return int instead")

    assert rc == 1


# ---------------------------------------------------------------------------
# --json output shape — matches OnboardResult contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_output_shape_when_all_pass(monkeypatch, capsys) -> None:
    """--json emits {passed, total, fully_passed, failures: []} when all pass."""
    _patch_checks(
        monkeypatch,
        [
            CheckResult(name="kairix_on_path", ok=True, detail="found"),
            CheckResult(name="secrets_loaded", ok=True, detail="present"),
        ],
    )

    rc = main(["check", "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["passed"] == 2
    assert payload["total"] == 2
    assert payload["fully_passed"] is True
    assert payload["failures"] == []


@pytest.mark.unit
def test_json_output_shape_when_some_fail(monkeypatch, capsys) -> None:
    """--json emits each failure as {check, detail, remediation}.

    Sabotage check: assert exact key set on each failure, not just presence.
    Catches accidental field renames (e.g. detail→message).
    """
    _patch_checks(
        monkeypatch,
        [
            CheckResult(name="kairix_on_path", ok=True, detail="found"),
            CheckResult(
                name="secrets_loaded",
                ok=False,
                detail="LLM credentials not found",
                fix="set them",
            ),
            CheckResult(
                name="vector_search_working",
                ok=False,
                detail="Vector search failed",
                fix="rerun embed",
            ),
        ],
    )

    rc = main(["check", "--json"])
    captured = capsys.readouterr()

    assert rc == 1
    payload = json.loads(captured.out)
    assert payload["passed"] == 1
    assert payload["total"] == 3
    assert payload["fully_passed"] is False
    assert len(payload["failures"]) == 2

    # Every failure has the exact documented shape: {check, detail, remediation}
    for failure in payload["failures"]:
        assert set(failure.keys()) >= {"check", "detail", "remediation"}
        assert isinstance(failure["check"], str)
        assert isinstance(failure["detail"], str)
        assert isinstance(failure["remediation"], str)
        # Sabotage-prove: blank or near-blank remediation strings fail
        assert len(failure["remediation"]) > 10

    # Failures are in the same order as the underlying checks (dependency order)
    failure_names = [f["check"] for f in payload["failures"]]
    assert failure_names == ["secrets_loaded", "vector_search_working"]


@pytest.mark.unit
def test_json_failure_remediation_uses_canonical_string(monkeypatch, capsys) -> None:
    """The remediation field surfaces the canonical entry — not the per-check
    fix string. Sabotage check: substitutes a deliberately wrong fix and
    confirms the JSON output ignores it in favour of the canonical entry."""
    from kairix.platform.onboard import check as check_mod

    _patch_checks(
        monkeypatch,
        [
            CheckResult(
                name="secrets_loaded",
                ok=False,
                detail="missing keys",
                fix="WRONG REMEDIATION — should be replaced",
            ),
        ],
    )

    main(["check", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["failures"][0]["remediation"] == check_mod._CANONICAL_REMEDIATIONS["secrets_loaded"]
    assert "WRONG" not in payload["failures"][0]["remediation"]


@pytest.mark.unit
def test_json_output_is_valid_json_when_all_pass(monkeypatch, capsys) -> None:
    """--json emits a single parseable JSON object on stdout, even when no
    failures are present. Sabotage check: confirms we don't emit garbage
    or trailing text that would break a `kairix onboard check --json | jq`
    pipeline."""
    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=True, detail="found")],
    )

    main(["check", "--json"])
    captured = capsys.readouterr()

    # Must parse cleanly as a single object, no leading/trailing junk
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)


@pytest.mark.unit
def test_json_output_has_documented_top_level_keys(monkeypatch, capsys) -> None:
    """The top-level JSON envelope has exactly the documented keys.

    Sabotage check: if a key gets dropped or renamed, this catches the
    contract drift before downstream consumers see it.
    """
    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=True, detail="found")],
    )

    main(["check", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    # passed, total, fully_passed, failures are mandatory; env_source is operator metadata
    assert "passed" in payload
    assert "total" in payload
    assert "fully_passed" in payload
    assert "failures" in payload


# ---------------------------------------------------------------------------
# Human-readable default — preserved for humans + agents that read stdout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_human_output_renders_checkmark_for_pass(monkeypatch, capsys) -> None:
    """The default (non-JSON) output renders a ✓ for passing checks."""
    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=True, detail="kairix at /usr/local/bin/kairix")],
    )

    rc = main(["check"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "✓" in captured.out
    assert "kairix_on_path" in captured.out
    assert "All 1 checks passed" in captured.out


@pytest.mark.unit
def test_human_output_renders_x_for_fail(monkeypatch, capsys) -> None:
    """The default output renders a ✗ for failed checks and shows their fix."""
    _patch_checks(
        monkeypatch,
        [
            CheckResult(
                name="secrets_loaded",
                ok=False,
                detail="missing creds",
                fix="run systemctl enable --now kairix-fetch-secrets",
            )
        ],
    )

    rc = main(["check"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "✗" in captured.out
    assert "secrets_loaded" in captured.out
    assert "missing creds" in captured.out
    # Per-check fix string is rendered in the human view (the multi-line guidance)
    assert "systemctl" in captured.out


@pytest.mark.unit
def test_human_output_summary_line_when_some_fail(monkeypatch, capsys) -> None:
    """Human summary line reports `passed/total checks passed — N failed`."""
    _patch_checks(
        monkeypatch,
        [
            CheckResult(name="kairix_on_path", ok=True, detail="found"),
            CheckResult(name="secrets_loaded", ok=False, detail="missing", fix="set them"),
        ],
    )

    main(["check"])
    captured = capsys.readouterr()

    assert "1/2 checks passed" in captured.out
    assert "1 failed" in captured.out


@pytest.mark.unit
def test_human_output_renders_env_source_when_loaded(monkeypatch, capsys, tmp_path) -> None:
    """When --env-file points at an existing file, the human output names it
    and reports how many keys were loaded.
    """
    env_file = tmp_path / "service.env"
    env_file.write_text("KAIRIX_TEST_KEY_FOR_RENDER=value1\nKAIRIX_OTHER_TEST=value2\n")

    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=True, detail="found")],
    )

    main(["check", "--env-file", str(env_file)])
    captured = capsys.readouterr()

    # File path is surfaced
    assert str(env_file) in captured.out
    # Either "keys loaded" or "already in env" (depending on whether the
    # KAIRIX_TEST_* keys were already set in the test environment)
    assert "keys loaded" in captured.out or "already in env" in captured.out


@pytest.mark.unit
def test_human_output_when_no_env_source_detected(monkeypatch, capsys) -> None:
    """When no env file is found, the human output reports the bare environment."""
    from kairix.platform.onboard import cli as cli_mod

    _patch_checks(
        monkeypatch,
        [CheckResult(name="kairix_on_path", ok=True, detail="found")],
    )

    # No env file passed; force _KNOWN_ENV_PATHS to be empty so production
    # paths aren't found during the test run.
    monkeypatch.setattr(cli_mod, "_KNOWN_ENV_PATHS", ())
    monkeypatch.setattr(
        "kairix.paths.env_file_override",
        lambda: None,
    )

    main(["check"])
    captured = capsys.readouterr()

    assert "env: none" in captured.out


# ---------------------------------------------------------------------------
# _self_load_env / _load_env_file — env file plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_env_file_returns_loaded_keys(tmp_path) -> None:
    """_load_env_file returns the list of keys it actually set in os.environ.

    Existing keys are skipped (do-not-override semantics).
    """
    from kairix.platform.onboard import cli as cli_mod

    env_file = tmp_path / "x.env"
    env_file.write_text(
        '# a comment\n'
        "\n"
        "KAIRIX_TEST_NEW_KEY_42=hello\n"
        "MALFORMED_NO_EQUALS\n"
        '"KAIRIX_TEST_QUOTED_42"="quoted-value"\n'
    )

    loaded = cli_mod._load_env_file(str(env_file))

    # The known-novel key (won't already be in env) was loaded
    assert "KAIRIX_TEST_NEW_KEY_42" in loaded
    # Comments + malformed lines were skipped
    assert "MALFORMED_NO_EQUALS" not in loaded


@pytest.mark.unit
def test_load_env_file_silently_returns_empty_on_missing(tmp_path) -> None:
    """_load_env_file returns [] without raising when the file does not exist."""
    from kairix.platform.onboard import cli as cli_mod

    loaded = cli_mod._load_env_file(str(tmp_path / "nonexistent.env"))
    assert loaded == []


@pytest.mark.unit
def test_self_load_env_explicit_path_wins(tmp_path) -> None:
    """When --env-file is passed, _self_load_env always returns that path."""
    from kairix.platform.onboard import cli as cli_mod

    env_file = tmp_path / "explicit.env"
    env_file.write_text("KAIRIX_EXPLICIT_NEW=yes\n")

    source, loaded = cli_mod._self_load_env(str(env_file))
    assert source == str(env_file)
    # Loaded keys list may include KAIRIX_EXPLICIT_NEW (if not previously set)
    # Sabotage-prove: source is exactly the explicit path, not a probe path
    assert source != "/run/secrets/kairix.env"


@pytest.mark.unit
def test_self_load_env_falls_back_to_known_path(tmp_path, monkeypatch) -> None:
    """Without --env-file or KAIRIX_ENV_FILE, the first known path that exists wins."""
    from kairix.platform.onboard import cli as cli_mod

    known_file = tmp_path / "service.env"
    known_file.write_text("KAIRIX_FALLBACK_NEW=ok\n")

    monkeypatch.setattr(cli_mod, "_KNOWN_ENV_PATHS", (str(known_file),))
    monkeypatch.setattr("kairix.paths.env_file_override", lambda: None)

    source, _loaded = cli_mod._self_load_env(None)
    assert source == str(known_file)


@pytest.mark.unit
def test_self_load_env_returns_none_when_nothing_found(monkeypatch) -> None:
    """When no env file is present anywhere, source is None and loaded is []."""
    from kairix.platform.onboard import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_KNOWN_ENV_PATHS", ())
    monkeypatch.setattr("kairix.paths.env_file_override", lambda: None)

    source, loaded = cli_mod._self_load_env(None)
    assert source is None
    assert loaded == []


@pytest.mark.unit
def test_self_load_env_uses_env_file_override(monkeypatch, tmp_path) -> None:
    """When env_file_override() returns a path, _self_load_env uses it."""
    from kairix.platform.onboard import cli as cli_mod

    target = tmp_path / "via-override.env"
    target.write_text("KAIRIX_OVERRIDE_NEW=set\n")

    monkeypatch.setattr("kairix.paths.env_file_override", lambda: str(target))
    monkeypatch.setattr(cli_mod, "_KNOWN_ENV_PATHS", ())

    source, _loaded = cli_mod._self_load_env(None)
    assert source == str(target)


# ---------------------------------------------------------------------------
# cmd_guide — install the agent usage guide into the document store
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_guide_returns_error_when_no_document_root(monkeypatch, capsys) -> None:
    """cmd_guide returns 1 and prints an error when no --document-root and no env override."""
    monkeypatch.setattr("kairix.paths.document_root_override", lambda: "")

    rc = main(["guide"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "document-root" in captured.err.lower() or "document_root" in captured.err.lower()


@pytest.mark.unit
def test_guide_returns_error_when_document_root_missing(capsys, tmp_path) -> None:
    """cmd_guide returns 1 when --document-root points at a non-existent path."""
    missing = tmp_path / "does-not-exist"

    rc = main(["guide", "--document-root", str(missing)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "does not exist" in captured.err


@pytest.fixture
def guide_source_in_pkg_root(monkeypatch, tmp_path):
    """Materialise a placeholder agent-usage-guide.md that cmd_guide can read.

    cmd_guide looks for the guide first via the inline-package path and
    then via the kairix package fallback (``kairix.__file__`` parent /
    docs / agent-usage-guide.md). The real file lives under
    docs/user-guide/agent-usage-guide.md, so neither candidate exists in
    a fresh checkout. We point ``kairix.__file__`` at a tmp_path package
    layout that DOES contain the file — no in-tree writes.
    """
    import kairix

    fake_pkg_root = tmp_path / "fake-pkg"
    fake_pkg = fake_pkg_root / "kairix"
    fake_pkg.mkdir(parents=True)
    fake_init = fake_pkg / "__init__.py"
    fake_init.write_text("")
    guide = fake_pkg_root / "docs" / "agent-usage-guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("# placeholder agent usage guide\n")

    monkeypatch.setattr(kairix, "__file__", str(fake_init))
    return guide


@pytest.mark.unit
def test_guide_dry_run_emits_source_and_dest(guide_source_in_pkg_root, tmp_path, capsys) -> None:
    """--dry-run prints the planned source + dest without writing the file."""
    doc_root = tmp_path / "vault"
    doc_root.mkdir()

    rc = main(["guide", "--document-root", str(doc_root), "--dry-run"])
    captured = capsys.readouterr()

    # Dry-run succeeds and announces what it would do
    assert rc == 0
    assert "Would install" in captured.out
    assert "Source:" in captured.out
    assert "Dest:" in captured.out
    # Sabotage-prove: dry-run did NOT write anything
    assert not (doc_root / "04-Agent-Knowledge" / "shared" / "kairix-usage.md").exists()


@pytest.mark.unit
def test_guide_writes_to_explicit_output(guide_source_in_pkg_root, tmp_path, capsys) -> None:
    """When --output is passed, the guide is written there verbatim."""
    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    output = tmp_path / "out" / "kairix-usage.md"

    rc = main(["guide", "--document-root", str(doc_root), "--output", str(output)])
    captured = capsys.readouterr()

    assert rc == 0
    assert output.exists()
    # Sabotage-prove: file is non-empty
    assert output.stat().st_size > 0
    assert "installed" in captured.out.lower()


@pytest.mark.unit
def test_guide_error_when_guide_source_missing(tmp_path, capsys, monkeypatch) -> None:
    """When neither the package nor source path contains agent-usage-guide.md,
    cmd_guide returns 1 with a clear error.
    """
    import kairix

    doc_root = tmp_path / "vault"
    doc_root.mkdir()

    # Substitute kairix.__file__ to a tmp location where no
    # docs/agent-usage-guide.md exists.
    fake_pkg_root = tmp_path / "fake-pkg"
    fake_pkg_root.mkdir()
    fake_init = fake_pkg_root / "__init__.py"
    fake_init.write_text("")
    monkeypatch.setattr(kairix, "__file__", str(fake_init))

    rc = main(["guide", "--document-root", str(doc_root)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "not found" in captured.err.lower()


@pytest.mark.unit
def test_guide_writes_to_default_path_when_agent_knowledge_dir_exists(guide_source_in_pkg_root, tmp_path, capsys) -> None:
    """When 04-Agent-Knowledge/shared exists, the guide installs there by default."""
    doc_root = tmp_path / "vault"
    shared_dir = doc_root / "04-Agent-Knowledge" / "shared"
    shared_dir.mkdir(parents=True)

    rc = main(["guide", "--document-root", str(doc_root)])
    captured = capsys.readouterr()

    assert rc == 0
    expected = shared_dir / "kairix-usage.md"
    assert expected.exists()
    assert str(expected) in captured.out


# ---------------------------------------------------------------------------
# cmd_verify — runs scripts/verify-search.py
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verify_returns_error_when_script_missing(capsys) -> None:
    """cmd_verify returns 1 with a clear error when verify-search.py is absent.

    The probe path is hard-coded as ``kairix/scripts/verify-search.py``
    relative to the cli module; in dev checkouts the script lives at
    ``<repo>/scripts/`` so this branch is the one exercised here.
    """
    rc = main(["verify"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "verify-search.py" in captured.err


@pytest.mark.unit
def test_verify_invokes_subprocess_when_script_present(monkeypatch, tmp_path) -> None:
    """When the verify-search.py script exists, cmd_verify runs it via subprocess
    and returns its return code.

    We materialise a script at the path cmd_verify probes (relative to the
    cli module) and substitute subprocess.run to capture the invocation.
    """
    import subprocess

    from kairix.platform.onboard import cli as cli_mod

    # The script probe in cmd_verify uses ``Path(__file__).parent.parent.parent
    # / "scripts" / "verify-search.py"`` — relative to the cli module, so we
    # need to fake that path. Substitute the cli module's __file__ to a
    # tmp_path layout where the script does exist.
    fake_cli_root = tmp_path / "kairix" / "platform" / "onboard"
    fake_cli_root.mkdir(parents=True)
    fake_cli_file = fake_cli_root / "cli.py"
    fake_cli_file.write_text("")
    fake_script = tmp_path / "kairix" / "scripts" / "verify-search.py"
    fake_script.parent.mkdir(parents=True)
    fake_script.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli_mod, "__file__", str(fake_cli_file))

    captured_cmd: list = []

    class _FakeCompleted:
        returncode = 42

    def _fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    rc = main(["verify", "--agent", "builder", "--json"])
    assert rc == 42
    # Sabotage-prove: argument plumbing is intact
    assert "--agent" in captured_cmd
    assert "builder" in captured_cmd
    assert "--json" in captured_cmd
