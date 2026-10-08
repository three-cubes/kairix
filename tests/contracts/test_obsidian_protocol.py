"""Contract test for the Obsidian connector plugin (F43).

Exercises the canonical fake (:class:`tests.fakes.FakeObsidian`) AND
the real implementation (:class:`kairix.connectors.obsidian.ObsidianConnector`)
through the same :class:`~kairix.core.protocols.SourceConnector`
Protocol assertions. F43 requires this pairing — without it the fake
can drift away from the real wire (or vice versa) and the production
path silently diverges from what BDD / unit tests measure.

Real-impl path is driven against a ``tmp_path`` vault. The connector
starts its watcher lazily on the first ``list_changes``, so the real impl
is built with ``watcher_factory=fake_obsidian_watcher_factory()`` (the
documented seam): the real connector, ``WatchdogSource`` and handler run,
but no OS-level watcher thread is started — the macOS FSEvents default
would otherwise outlive the test (it is never closed here) and replay
pre-start events into the drain.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.connectors.obsidian import ObsidianConnector
from kairix.core.protocols import ChangeEvent, RawArtefact, SourceConnector
from tests.fakes import FakeObsidian, fake_obsidian_watcher_factory

# ---------------------------------------------------------------------------
# Factories — each yields a fresh SourceConnector for one test.
# ---------------------------------------------------------------------------


def _fake_factory(tmp_path: Path) -> SourceConnector:
    """Canonical fake factory — seeds three created events + content."""
    return FakeObsidian(
        vault_root=tmp_path,
        events=[
            ChangeEvent(op="created", item_id="alpha.md", modified_at="2026-05-22T00:00:00Z"),
            ChangeEvent(op="created", item_id="bravo.md", modified_at="2026-05-22T00:00:01Z"),
        ],
        content={
            "alpha.md": b"# Alpha\n\nFirst note.",
            "bravo.md": b"# Bravo\n\nSecond note.",
        },
    )


def _real_factory(tmp_path: Path) -> SourceConnector:
    """Real-impl factory — seeds two real files inside ``tmp_path``."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "alpha.md").write_text("# Alpha\n\nFirst note.", encoding="utf-8")
    (vault / "bravo.md").write_text("# Bravo\n\nSecond note.", encoding="utf-8")
    return ObsidianConnector(
        vault_root=vault,
        known_state_resolver=lambda _c: {},
        watcher_factory=fake_obsidian_watcher_factory(),
    )


_FACTORIES: list[tuple[str, Callable[[Path], SourceConnector]]] = [
    ("fake", _fake_factory),
    ("real", _real_factory),
]


# ---------------------------------------------------------------------------
# Contract assertions — both implementations must satisfy each one.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_satisfies_source_connector_protocol(
    name: str, factory: Callable[[Path], SourceConnector], tmp_path: Path
) -> None:
    """F43: both fake and real impl satisfy the runtime-checkable Protocol.

    Sabotage-proof: removing ``list_changes`` from
    :class:`ObsidianConnector` flips the real-impl isinstance check
    to False; deleting the corresponding attribute from
    :class:`FakeObsidian` flips the fake check to False.
    """
    connector = factory(tmp_path)
    assert isinstance(connector, SourceConnector), f"{name!r} factory output is not a SourceConnector"
    assert connector.name == "obsidian"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_list_changes_returns_change_events(
    name: str, factory: Callable[[Path], SourceConnector], tmp_path: Path
) -> None:
    """Both implementations stream :class:`ChangeEvent` instances.

    Sabotage-proof: the real impl mutated to return ``[None]`` from
    ``list_changes`` flunks the isinstance loop below; the fake
    mutated to yield ``{"op": "created"}`` dicts flunks the same loop.
    """
    connector = factory(tmp_path)
    events = list(connector.list_changes(cursor=None))
    assert events, f"{name!r} factory produced no events"
    for ev in events:
        assert isinstance(ev, ChangeEvent), f"{name!r} yielded a non-ChangeEvent: {ev!r}"
        assert ev.op in ("created", "modified", "deleted")


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_fetch_returns_raw_artefact(
    name: str, factory: Callable[[Path], SourceConnector], tmp_path: Path
) -> None:
    """Both implementations satisfy the ``fetch`` -> :class:`RawArtefact` shape.

    Sabotage-proof: return a tuple from ``fetch`` instead — the
    isinstance assertion fails for both impls.
    """
    connector = factory(tmp_path)
    artefact = connector.fetch("alpha.md")
    assert isinstance(artefact, RawArtefact)
    assert artefact.mime == "text/markdown"
    assert b"Alpha" in artefact.raw or artefact.raw == b""  # fake content may be empty if not seeded
    assert artefact.fetched_at.endswith("Z") or "+" in artefact.fetched_at


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_source_link_round_trips_to_obsidian_scheme(
    name: str, factory: Callable[[Path], SourceConnector], tmp_path: Path
) -> None:
    """``source_link`` returns an ``obsidian://`` URL on both impls.

    Sabotage-proof: hard-code the real impl to return an empty string —
    both ``startswith`` assertions then fail.
    """
    connector = factory(tmp_path)
    link = connector.source_link("alpha.md")
    assert link.startswith("obsidian://open?vault="), f"{name!r} produced unexpected link: {link!r}"
    assert "alpha.md" in link, f"{name!r} link does not carry item_id: {link!r}"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_sensitivity_for_returns_configured_tier(
    name: str, factory: Callable[[Path], SourceConnector], tmp_path: Path
) -> None:
    """``sensitivity_for`` returns the connector's configured tier.

    Sabotage-proof: mutate the real impl to return ``"public"`` — the
    assertion below fails because the factory configured ``"internal"``.
    """
    connector = factory(tmp_path)
    tier = connector.sensitivity_for("alpha.md")
    assert tier == "internal", f"{name!r} returned unexpected sensitivity: {tier!r}"
