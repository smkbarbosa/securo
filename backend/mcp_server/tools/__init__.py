"""Auto-imports all tool modules so they register themselves into the
shared registry on startup.
"""
from mcp_server.tools import (  # noqa: F401
    transactions,
    accounts,
    categories,
    payees,
    budgets,
    debts,
    reports,
    search,
    aggregate,
    proposals,
    knowledge,
    lifecycle,
    groups,
)
