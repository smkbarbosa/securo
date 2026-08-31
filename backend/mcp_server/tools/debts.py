"""Read-only debt tools: what's owed, and what a realistic payoff plan
looks like given actual income/expense history.

Nothing here writes to the database — see `proposals.py` for
`propose_debt_payoff_strategy` and `propose_debt_reserve_goal`, which
turn this analysis into a change the user can confirm.
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import debt_payoff_strategy_service, debt_service, report_service
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, resolve_workspace_id


@tool(
    name="list_debts",
    description=(
        "List the workspace's tracked debts (loans, payroll-deducted loans, "
        "overdue credit cards) with their current balance and active payoff "
        "plan, if any."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "negotiating", "paid_off", "defaulted"],
                "description": "Filter to one status. Omit to return all.",
            },
        },
        "additionalProperties": False,
    },
    tags=["read", "debts"],
)
async def list_debts(*, session: AsyncSession, ctx: CallContext, status: str | None = None) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    debts = await debt_service.get_debts(session, ws_id)
    if status:
        debts = [d for d in debts if d.status == status]

    def _plan(d) -> dict[str, Any] | None:
        active = next((p for p in d.plans if p.status == "active"), None)
        if active is None:
            return None
        return {
            "collection_mode": active.collection_mode,
            "installment_amount": num(active.installment_amount),
            "interest_rate": num(active.interest_rate),
            "frequency": active.frequency,
            "installments_paid": sum(1 for i in active.installments if i.status == "paid"),
            "installments_total": len(active.installments),
        }

    return {
        "debts": [
            {
                "id": str(d.id),
                "kind": d.kind,
                "creditor_name": d.creditor_name,
                "status": d.status,
                "current_balance": num(d.current_balance),
                "original_principal": num(d.original_principal),
                "currency": d.currency,
                "active_plan": _plan(d),
            }
            for d in debts
        ],
    }


@tool(
    name="analyze_debt_payoff",
    description=(
        "Analyze recent monthly income vs. expenses together with active debts "
        "to suggest a payoff strategy: how much monthly surplus is realistically "
        "available for extra debt payments, projected payoff timelines under "
        "snowball vs. avalanche with that surplus, and an alternative of setting "
        "the same money aside (e.g. in a savings goal) toward negotiating a "
        "lump-sum settlement instead of paying installments down gradually. "
        "Use this before propose_debt_payoff_strategy or propose_debt_reserve_goal "
        "so the numbers you propose are grounded in the user's actual cash flow."
    ),
    parameters={
        "type": "object",
        "properties": {
            "months": {
                "type": "integer",
                "minimum": 1,
                "maximum": 24,
                "default": 3,
                "description": "How many recent months of income/expense history to average.",
            },
        },
        "additionalProperties": False,
    },
    tags=["read", "debts"],
)
async def analyze_debt_payoff(*, session: AsyncSession, ctx: CallContext, months: int = 3) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)

    rep = await report_service.get_income_expenses_report(
        session, ws_id, ctx.user_id, months=int(months), interval="monthly", currency="USD"
    )
    # The trend's last point is always the current, still-in-progress
    # calendar month — averaging it in would dilute the result toward
    # zero early in the month. Only complete months count.
    current_period = date.today().strftime("%Y-%m")
    complete_periods = [dp for dp in rep.trend if dp.date != current_period]
    incomes = [dp.breakdowns.get("income", 0.0) for dp in complete_periods]
    expenses = [dp.breakdowns.get("expenses", 0.0) for dp in complete_periods]
    avg_income = sum(incomes) / len(incomes) if incomes else 0.0
    avg_expenses = sum(expenses) / len(expenses) if expenses else 0.0

    debts = await debt_service.get_debts(session, ws_id)
    active_debts = [d for d in debts if d.status == "active"]

    committed_installments = 0.0
    debt_summaries = []
    for d in active_debts:
        active_plan = next((p for p in d.plans if p.status == "active"), None)
        if active_plan is not None:
            committed_installments += float(active_plan.installment_amount)
        debt_summaries.append(
            {
                "debt_id": str(d.id),
                "creditor_name": d.creditor_name,
                "current_balance": num(d.current_balance),
                "collection_mode": active_plan.collection_mode if active_plan else None,
                "installment_amount": num(active_plan.installment_amount) if active_plan else None,
                "interest_rate": num(active_plan.interest_rate) if active_plan else None,
            }
        )

    # Conservative on purpose: every active plan's installment is subtracted
    # even though a manual-mode payment may already be inside avg_expenses
    # (if it was paid and linked to a real transaction in the lookback
    # window) — better to understate the free surplus than overstate it.
    available_surplus = max(0.0, avg_income - avg_expenses - committed_installments)
    surplus_decimal = Decimal(str(round(available_surplus, 2)))

    projections = {}
    for method in ("avalanche", "snowball"):
        proj = await debt_payoff_strategy_service.simulate_payoff(session, ws_id, method, surplus_decimal)
        projections[method] = proj.model_dump(mode="json")

    # Reserve/lump-sum alternative: prioritize a debt already being
    # negotiated, else the smallest balance — the classic candidate for
    # "quitação à vista com desconto" instead of gradual installments.
    negotiating = [d for d in active_debts if d.status == "negotiating"]
    target_debt = negotiating[0] if negotiating else (
        min(active_debts, key=lambda d: d.current_balance) if active_debts else None
    )
    reserve_alternative = None
    if target_debt is not None and available_surplus > 0:
        months_needed = math.ceil(float(target_debt.current_balance) / available_surplus)
        reserve_alternative = {
            "debt_id": str(target_debt.id),
            "creditor_name": target_debt.creditor_name,
            "target_amount": num(target_debt.current_balance),
            "suggested_monthly_contribution": round(available_surplus, 2),
            "months_to_reach_target": months_needed,
            "note": (
                "Instead of paying this debt down gradually, set this amount aside "
                "each month (e.g. in a savings goal or short-term, liquid investment) "
                "to negotiate a lump-sum settlement once the target is reached — often "
                "available at a discount for defaulted or already-negotiating debts."
            ),
        }

    return {
        "lookback_months": int(months),
        "avg_monthly_income": round(avg_income, 2),
        "avg_monthly_expenses": round(avg_expenses, 2),
        "committed_debt_installments": round(committed_installments, 2),
        "available_monthly_surplus": round(available_surplus, 2),
        "surplus_assumption": (
            "available_monthly_surplus = avg_monthly_income - avg_monthly_expenses - "
            "committed_debt_installments (sum of every active plan's installment_amount). "
            "This is a conservative floor, not a precise number."
        ),
        "active_debts": debt_summaries,
        "projections": projections,
        "reserve_alternative": reserve_alternative,
    }
