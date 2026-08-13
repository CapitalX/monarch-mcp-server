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
# established field-by-field against the live API, then reconciled against the
# web app's own GoalSummaryFields fragment. Note the balance field is
# `currentBalance`, not `currentAmount`, and progress is served directly as a
# 0..1 fraction rather than being computed client-side.
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
    status
    priority
    progress
    currentBalance
    targetAmount
    targetDate
    plannedMonthlyContribution
    estimatedMonthsUntilCompletion
    forecastedCompletionDate
    isSinkingFund
    createdAt
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
                "status": g.get("status"),
                "priority": g.get("priority"),
                "current_balance": g.get("currentBalance"),
                "progress_percent": (
                    round(g["progress"] * 100, 1)
                    if isinstance(g.get("progress"), (int, float)) else None
                ),
                "target_amount": g.get("targetAmount"),
                "target_date": g.get("targetDate"),
                "planned_monthly_contribution": g.get("plannedMonthlyContribution"),
                "estimated_months_until_completion": g.get(
                    "estimatedMonthsUntilCompletion"),
                "forecasted_completion_date": g.get("forecastedCompletionDate"),
                "is_sinking_fund": g.get("isSinkingFund"),
                "created_at": g.get("createdAt"),
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


UPDATE_SAVINGS_GOAL_MUTATION = gql("""
mutation Common_UpdateSavingsGoal($input: UpdateSavingsGoalInput!) {
  updateSavingsGoal(input: $input) {
    savingsGoal {
      id
      name
      targetAmount
      targetDate
      plannedMonthlyContribution
      priority
      __typename
    }
    errors {
      message
      code
      fieldErrors {
        field
        messages
        __typename
      }
      __typename
    }
    __typename
  }
}
""")


@mcp.tool()
async def update_savings_goal(
    goal_id: str,
    target_amount: Optional[float] = None,
    target_date: Optional[str] = None,
    name: Optional[str] = None,
    priority: Optional[int] = None,
) -> str:
    """
    Update a savings goal's target or monthly contribution.

    Unlike the transaction-rule mutation, this is a genuine partial update:
    omitted fields are preserved, verified against the live API. Pass only what
    you want to change.

    NOTE: the monthly contribution cannot be changed here. UpdateSavingsGoalInput
    accepts `plannedMonthlyContribution` and reports success, but the value does
    not persist -- it mirrors the goal's budget entry for the month
    (`currentMonthPlannedContributionAmount`), not a field on the goal. No
    reachable mutation sets it: the budget-item mutation rejects both `goalId`
    and `savingsGoalId`. Contribution targets have to be set in the Monarch app.
    The argument is deliberately absent rather than accepted-and-ignored.

    Args:
        goal_id: Goal to update. Use get_goals -- and note these are
            `savingsGoals` ids, which differ from the legacy goalsV2 ids.
        target_amount: Total amount the goal is aiming at.
        target_date: Target completion date, "YYYY-MM-DD". Optional on a goal.
        name: Rename the goal.
        priority: Ordering among goals, lower first.

    Returns:
        JSON with the goal's state after the update.
    """
    try:
        changes: Dict[str, Any] = {}
        if target_amount is not None:
            changes["targetAmount"] = target_amount
        if target_date is not None:
            changes["targetDate"] = target_date
        if name is not None:
            changes["name"] = name
        if priority is not None:
            changes["priority"] = priority

        if not changes:
            return json_success({
                "success": False,
                "message": "Nothing to update -- pass at least one field.",
            })

        client = await get_monarch_client()
        result = await client.gql_call(
            operation="Common_UpdateSavingsGoal",
            graphql_query=UPDATE_SAVINGS_GOAL_MUTATION,
            variables={"input": {"id": goal_id, **changes}},
        )

        payload = result.get("updateSavingsGoal") or {}
        errors = payload.get("errors")
        if errors:
            meaningful = {
                k: v for k, v in errors.items()
                if k != "__typename" and v is not None
            }
            return json_success({
                "success": False,
                "errors": meaningful or {
                    "message": "Monarch rejected the update without a reason"
                },
            })

        goal = payload.get("savingsGoal") or {}
        return json_success({
            "success": True,
            "goal_id": goal_id,
            "changed": sorted(changes),
            "goal": {
                "id": goal.get("id"),
                "name": goal.get("name"),
                "target_amount": goal.get("targetAmount"),
                "target_date": goal.get("targetDate"),
                "planned_monthly_contribution": goal.get(
                    "plannedMonthlyContribution"),
                "priority": goal.get("priority"),
            },
        })
    except Exception as e:
        return json_error("update_savings_goal", e)
