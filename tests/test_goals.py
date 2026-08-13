"""Tests for goals MCP tools."""

import json
from unittest.mock import AsyncMock, patch

from monarch_mcp_server.tools.goals import get_goals


def _goal(**overrides):
    goal = {
        "id": "goal_1",
        "name": "Rainy Day Fund",
        "defaultName": "Emergency fund",
        "objective": "emergency_fund",
        "type": "asset",
        "priority": 0,
        "targetAmount": 10000.0,
        "currentAmount": 4000.0,
        "startingAmount": None,
        "plannedMonthlyContribution": 0.0,
        "archivedAt": None,
        "completedAt": None,
        "accountAllocations": [
            {"id": "alloc_1", "account": {"id": "acc_1", "displayName": "Savings",
                                          "currentBalance": 4000.0}}
        ],
    }
    goal.update(overrides)
    return goal


def _client(goals):
    client = AsyncMock()
    client.gql_call.return_value = {"goalsV2": goals}
    return client


class TestGetGoals:
    """Tests for get_goals tool."""

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_get_goals_success(self, mock_get_client):
        """Goals are returned with their allocated accounts."""
        mock_get_client.return_value = _client([_goal()])

        data = json.loads(await get_goals())

        assert data["count"] == 1
        goal = data["goals"][0]
        assert goal["name"] == "Rainy Day Fund"
        assert goal["default_name"] == "Emergency fund"
        assert goal["accounts"][0]["name"] == "Savings"

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_asset_goal_progress(self, mock_get_client):
        """Asset progress runs from startingAmount up to targetAmount."""
        mock_get_client.return_value = _client([_goal()])

        data = json.loads(await get_goals())

        assert data["goals"][0]["progress_percent"] == 40.0

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_debt_goal_progress(self, mock_get_client):
        """Debt progress measures the amount paid down, not current/target.

        A debt goal runs from a negative startingAmount up to a targetAmount of
        0, so the asset formula would report a nonsensical value.
        """
        mock_get_client.return_value = _client([_goal(
            type="debt", targetAmount=0.0,
            startingAmount=-1000.0, currentAmount=-250.0,
        )])

        data = json.loads(await get_goals())

        assert data["goals"][0]["progress_percent"] == 75.0

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_progress_none_when_undefined(self, mock_get_client):
        """A zero-width range yields None rather than a divide-by-zero."""
        mock_get_client.return_value = _client([_goal(
            targetAmount=0.0, startingAmount=0.0, currentAmount=0.0,
        )])

        assert json.loads(await get_goals())["goals"][0]["progress_percent"] is None

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_archived_included_by_default(self, mock_get_client):
        """Archived goals still hold accounts and balances, so they are kept."""
        mock_get_client.return_value = _client([
            _goal(archivedAt="2025-12-16T12:36:36+00:00")
        ])

        data = json.loads(await get_goals())

        assert data["count"] == 1
        assert data["goals"][0]["archived"] is True

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_archived_can_be_excluded(self, mock_get_client):
        """include_archived=False filters them out."""
        mock_get_client.return_value = _client([
            _goal(archivedAt="2025-12-16T12:36:36+00:00")
        ])

        data = json.loads(await get_goals(include_archived=False))

        assert data["count"] == 0

    @patch('monarch_mcp_server.tools.goals.get_monarch_client')
    async def test_get_goals_error(self, mock_get_client):
        """Transport failures are reported as tool errors."""
        client = AsyncMock()
        client.gql_call.side_effect = Exception("boom")
        mock_get_client.return_value = client

        data = json.loads(await get_goals())

        assert data["error"] is True
        assert data["tool"] == "get_goals"
