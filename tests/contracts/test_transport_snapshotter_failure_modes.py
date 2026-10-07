"""F68 failure-mode contract for the ``TransportSnapshotter`` Protocol.

``kairix probe-config`` reads transport-layer stats from the snapshotter
after the probe run. The failure shape that matters is the EMPTY
snapshot — a provider that never exercised the kairix transport layer.
The report must keep its uniform schema (every stage key present, the
transport section zeroed) and must not invent tuning recommendations
from stats nobody observed.

F43 parity: ONE body runs over the real ``NullTransportSnapshotter``
(production's no-transport-observed snapshotter) and the canonical
``FakeTransportSnapshotter`` left at its empty default.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.quality.probe.config_runner import NullTransportSnapshotter, TransportSnapshotter, run_probe_config
from tests.fakes import FakeProvider, FakeTransportSnapshotter

pytestmark = pytest.mark.contract

_UNIFORM_STAGES = {"pool_acquire", "coalesce_wait", "cache_lookup", "http_roundtrip", "response_parse"}


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", NullTransportSnapshotter),
        ("fake", FakeTransportSnapshotter),
    ],
)
def test_snapshot_returns_empty_report_keeps_uniform_shape_and_no_recommendations(
    name: str, factory: Callable[[], TransportSnapshotter]
) -> None:
    """``returns_empty``: an empty snapshot yields a zeroed transport
    section, every uniform stage key, and no tuning recommendations.

    Sabotage proof (executed): in ``_build_recommendations`` change the
    cache trigger to ``if transport.cache_hit_rate < CACHE_HIT_RATE_RECOMMEND``
    (dropping the ``0.0 <`` lower bound) → both cases fail (a
    recommendation is invented for a cache nobody exercised). Restored.
    """
    report = run_probe_config(
        FakeProvider(),
        snapshotter=factory(),
        warm_samples=2,
        concurrency=2,
        repeated_samples=2,
    )
    assert report.status == "healthy", f"{name}: {report.status} {report.warnings}"
    assert (
        report.transport.coalesce_ratio,
        report.transport.cache_hit_rate,
        report.transport.pool_acquire_p50_ms,
    ) == (0.0, 0.0, 0.0)
    assert set(report.stage_latency_ms) == _UNIFORM_STAGES
    assert report.tuning_recommendations == []
