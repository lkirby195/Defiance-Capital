"""FastAPI application: webhook receiver and review-queue API.  # SPEC §2"""

from __future__ import annotations

from fastapi import FastAPI

from api.routes.deals import router as deals_router
from api.routes.intake import router as intake_router

app = FastAPI(title="glenwood-uw", version="0.1.0")
app.include_router(intake_router)
app.include_router(deals_router)
