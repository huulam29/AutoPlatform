"""Costs API (agentline.costs).

GET /costs?days=30      spend by day, pipeline, model and node, plus this month and the budget
PUT /budget {monthly_usd}   (null or 0 removes it)
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from agentline import costs


class BudgetBody(BaseModel):
    monthly_usd: float | None = None


def create_costs_router(app: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/costs")
    async def get_costs(days: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(costs.report, app, max(1, min(days, 365)))

    @router.put("/budget")
    def put_budget(body: BudgetBody) -> dict[str, Any]:
        return {"budget_usd": costs.set_budget(body.monthly_usd).get("monthly_usd")}

    return router
