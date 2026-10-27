from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix="/health")


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def ready(request: Request) -> dict[str, str] | JSONResponse:
    try:
        async with request.app.state.db.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError, OSError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}
