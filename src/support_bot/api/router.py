from __future__ import annotations

from fastapi import APIRouter

from support_bot.api.routes import health, me, telegram, tickets, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(telegram.router)
api_router.include_router(me.router)
api_router.include_router(tickets.router)
api_router.include_router(users.router)
