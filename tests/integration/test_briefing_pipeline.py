"""
Integration tests: briefing source fetcher against synthetic agent memory logs.
"""

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
def test_briefing_fetches_memory_logs(real_agents_config):
    """Briefing source fetcher finds synthetic agent memory logs."""
    from kairix.agents.briefing.sources import fetch_memory_logs

    content = fetch_memory_logs("agent-alpha", max_tokens=500, config=real_agents_config)
    assert content  # non-empty string
    assert "pending" in content.lower() or "session" in content.lower()


@pytest.mark.integration
def test_briefing_warns_for_nonexistent_agent(real_agents_config):
    """Briefing returns empty string for agent with no memory dir."""
    from kairix.agents.briefing.sources import fetch_memory_logs

    content = fetch_memory_logs("nonexistent_agent_xyz", max_tokens=500, config=real_agents_config)
    assert content == ""
