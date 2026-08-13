"""Tests for goals MCP tools."""

import json
from unittest.mock import AsyncMock, patch

from monarch_mcp_server.tools.goals import get_goals


def _goal(**overrides):
    goal = {
        "id": "sg_1",
        "name": "Rainy Day Fund",
        "type": "emergency_fund",
        "priority": 2,
        "targetAmount": 7500.0,
        "targetDate": "2026-12-31",
        "plannedMonthlyContribution": 100.0,
        "archivedAt": None,
        "completedAt": None,
    }
    goal.update(overrides)
    return goal


def _client(goals):
    c = AsyncMock()
    c.gql_call.return_value = {"savingsGoals": goals}
    return c


class TestGetGoals:
    """Tests for get_goals tool."""

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_reads_savings_goals_not_goalsv2(self, mock_get_client):
        """The tool must query savingsGoals.

        goalsV2 is the superseded collection and serves pre-migration numbers.
        """
        client = _client([_goal()])
        mock_get_client.return_value = client

        data = json.loads(await get_goals())

        assert data["count"] == 1
        assert data["goals"][0]["target_amount"] == 7500.0
        assert data["goals"][0]["target_date"] == "2026-12-31"
        # The operation name is the accessible signal that the right
        # collection was queried; the query itself is a parsed DocumentNode.
        assert client.gql_call.call_args.kwargs["operation"] == "GetSavingsGoals"

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_goalsv2_payload_yields_nothing(self, mock_get_client):
        """Guards against silently reading the wrong collection."""
        c = AsyncMock()
        c.gql_call.return_value = {"goalsV2": [_goal()]}
        mock_get_client.return_value = c

        assert json.loads(await get_goals())["count"] == 0

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_archived_at_is_raw_and_never_filtered(self, mock_get_client):
        """archivedAt is passed through; nothing is hidden from the caller."""
        mock_get_client.return_value = _client([
            _goal(), _goal(id="sg_2", archivedAt="2026-01-01T00:00:00+00:00"),
        ])

        data = json.loads(await get_goals())

        assert data["count"] == 2
        assert "archived" not in data["goals"][0]

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_error_handling(self, mock_get_client):
        """Transport failures are reported as tool errors."""
        c = AsyncMock()
        c.gql_call.side_effect = Exception("boom")
        mock_get_client.return_value = c

        data = json.loads(await get_goals())

        assert data["error"] is True
        assert data["tool"] == "get_goals"
