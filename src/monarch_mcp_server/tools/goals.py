"""Goals tools with GraphQL queries."""

from __future__ import annotations

import logging
from typing import Any, Dict

from gql import gql

from monarch_mcp_server.app import mcp
from monarch_mcp_server.client import get_monarch_client
from monarch_mcp_server.helpers import json_success, json_error

logger = logging.getLogger(__name__)

# Monarch disables GraphQL introspection for non-admin users and masks unknown
# field errors as a generic "Something went wrong", so this selection set was
# established field-by-field against the live API. Fields confirmed absent on
# SavingsGoal (do not re-add without re-testing): currentAmount, objective,
# icon, startingAmount, completionPercent, accountAllocations.
#
# Note there is no current-balance field here, so this reports the target and
# planned contribution but cannot compute progress.
#
# There are TWO goal collections. `savingsGoals` is the current one and matches
# what the app shows. `goalsV2` is the superseded collection: on a real account
# every goalsV2 record carried an identical archivedAt to the microsecond (the
# migration timestamp) while the same goals showed as active in the app, and
# their targets were stale -- a goal reading 10000 in goalsV2 was 7500 in
# savingsGoals. Query savingsGoals; goalsV2 will quietly serve pre-migration
# numbers.
#
# The two also have separate ids for the same goal, which is why a transaction
# rule has both linkGoalAction and linkSavingsGoalAction and they are NOT
# interchangeable.
GET_GOALS_QUERY = gql("""
query GetSavingsGoals {
  savingsGoals {
    id
    name
    type
    priority
    targetAmount
    targetDate
    plannedMonthlyContribution
    archivedAt
    completedAt
    __typename
  }
}
""")


@mcp.tool()
async def get_goals() -> str:
    """
    List Monarch savings and debt-paydown goals.

    Use this to find a goal id for `link_goal_id` on a transaction rule, or to
    report on goal progress.

    `name` is user-editable while `default_name` is the template the goal was
    created from -- a goal renamed "End Game" still reports default_name
    "Retirement" and objective "retirement", which is the reliable thing to
    match on programmatically.

    Every goal is returned. `archived_at` is passed through raw and should not
    be read as the app's archive state -- see the comment above the query.

    Returns:
        JSON list of goals with progress and the accounts allocated to each.
    """
    try:
        client = await get_monarch_client()
        result = await client.gql_call(
            operation="GetSavingsGoals", graphql_query=GET_GOALS_QUERY, variables={}
        )

        goals = []
        for g in result.get("savingsGoals") or []:
            goals.append({
                "id": g.get("id"),
                "name": g.get("name"),
                "type": g.get("type"),
                "priority": g.get("priority"),
                "target_amount": g.get("targetAmount"),
                "target_date": g.get("targetDate"),
                "planned_monthly_contribution": g.get("plannedMonthlyContribution"),
                "archived_at": g.get("archivedAt"),
                "completed_at": g.get("completedAt"),
            })

        return json_success({
            "count": len(goals),
            "note": (
                "Goals track allocated ACCOUNT BALANCES, not categorized "
                "transactions. A transaction rule only touches a goal via "
                "link_goal_id, which Monarch requires account_ids alongside. "
                "`archived_at` is raw and does not match the app's archive "
                "state -- do not filter on it."
            ),
            "goals": goals,
        })
    except Exception as e:
        return json_error("get_goals", e)
