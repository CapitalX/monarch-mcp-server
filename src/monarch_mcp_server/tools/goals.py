"""Goals tools with GraphQL queries."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from gql import gql

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_success, json_error

logger = logging.getLogger(__name__)

# Monarch disables GraphQL introspection for non-admin users and masks unknown
# field errors as a generic "Something went wrong", so this selection set was
# established field-by-field against the live API. Fields confirmed absent (do
# not re-add without re-testing): targetDate, createdAt, updatedAt, goalBalance,
# balance, progress, completedPercent.
GET_GOALS_QUERY = gql("""
query GetGoalsV2 {
  goalsV2 {
    id
    name
    defaultName
    objective
    type
    priority
    targetAmount
    currentAmount
    startingAmount
    plannedMonthlyContribution
    archivedAt
    completedAt
    accountAllocations {
      id
      account {
        id
        displayName
        currentBalance
        __typename
      }
      __typename
    }
    __typename
  }
}
""")


def _progress(goal: Dict[str, Any]) -> Optional[float]:
    """Percent complete, or None when it cannot be computed meaningfully.

    Asset goals run from startingAmount (or 0) up to targetAmount. Debt goals
    run from a negative startingAmount up to a targetAmount of 0, so the naive
    current/target ratio is wrong for them and is computed against the amount
    paid down instead.
    """
    target = goal.get("targetAmount")
    current = goal.get("currentAmount")
    start = goal.get("startingAmount")
    if current is None or target is None:
        return None

    if goal.get("type") == "debt":
        if start is None or start == target:
            return None
        return round((start - current) / (start - target) * 100, 1)

    base = start or 0.0
    if target == base:
        return None
    return round((current - base) / (target - base) * 100, 1)


@mcp.tool()
async def get_goals(include_archived: bool = True) -> str:
    """
    List Monarch savings and debt-paydown goals.

    Use this to find a goal id for `link_goal_id` on a transaction rule, or to
    report on goal progress.

    `name` is user-editable while `default_name` is the template the goal was
    created from -- a goal renamed "End Game" still reports default_name
    "Retirement" and objective "retirement", which is the reliable thing to
    match on programmatically.

    Args:
        include_archived: Include archived goals. Defaults to True, because an
            archived goal still holds its accounts and balances and is easy to
            miss otherwise -- a goal that looks "missing" is usually archived.

    Returns:
        JSON list of goals with progress and the accounts allocated to each.
    """
    try:
        client = await get_monarch_client()
        result = await client.gql_call(
            operation="GetGoalsV2", graphql_query=GET_GOALS_QUERY, variables={}
        )

        goals = []
        for g in result.get("goalsV2") or []:
            archived = bool(g.get("archivedAt"))
            if archived and not include_archived:
                continue
            goals.append({
                "id": g.get("id"),
                "name": g.get("name"),
                "default_name": g.get("defaultName"),
                "objective": g.get("objective"),
                "type": g.get("type"),
                "priority": g.get("priority"),
                "target_amount": g.get("targetAmount"),
                "current_amount": g.get("currentAmount"),
                "starting_amount": g.get("startingAmount"),
                "planned_monthly_contribution": g.get("plannedMonthlyContribution"),
                "progress_percent": _progress(g),
                "archived": archived,
                "archived_at": g.get("archivedAt"),
                "completed_at": g.get("completedAt"),
                "accounts": [
                    {
                        "account_id": (a.get("account") or {}).get("id"),
                        "name": (a.get("account") or {}).get("displayName"),
                        "current_balance": (a.get("account") or {}).get(
                            "currentBalance"
                        ),
                    }
                    for a in (g.get("accountAllocations") or [])
                ],
            })

        return json_success({
            "count": len(goals),
            "note": (
                "Goals track allocated ACCOUNT BALANCES, not categorized "
                "transactions. A transaction rule only touches a goal via "
                "link_goal_id, which Monarch requires account_ids alongside."
            ),
            "goals": goals,
        })
    except Exception as e:
        return json_error("get_goals", e)
