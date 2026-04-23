"""Router registry.

Anti-drift rule (D1): every HTTP route lives in a module under `routes/` and
is appended to `ALL_ROUTERS` here. The app factory iterates this list to
mount routers — no direct imports from `app.py`, no scattered
`app.include_router(...)` calls. Adding a route is:

1. Create `etfpulse/api/routes/foo.py` with `router = APIRouter()`
2. Import it below and append to `ALL_ROUTERS`

That's it. `app.py` never changes.
"""

from __future__ import annotations

from fastapi import APIRouter

from etfpulse.api.routes.admin import router as admin_router
from etfpulse.api.routes.health import router as health_router
from etfpulse.api.routes.signals import router as signals_router
from etfpulse.api.routes.telegram import router as telegram_router

ALL_ROUTERS: list[APIRouter] = [
    health_router,
    admin_router,
    telegram_router,
    signals_router,
]
