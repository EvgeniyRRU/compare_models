"""Mini App REST API router: aggregates the ticket and user routers."""

from fastapi import APIRouter

from app.api.routes import tickets, users

api_router = APIRouter(prefix="/api")
api_router.include_router(tickets.router)
api_router.include_router(users.router)
