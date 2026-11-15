"""Mini App REST API router: aggregates the me, ticket, and user routers."""

from fastapi import APIRouter

from app.api.routes import me, tickets, users

api_router = APIRouter(prefix="/api")
api_router.include_router(me.router)
api_router.include_router(tickets.router)
api_router.include_router(users.router)
