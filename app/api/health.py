from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=None)
async def ready(request: Request) -> JSONResponse | dict[str, str]:
    try:
        async with request.app.state.db.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "not ready"}, status_code=503)
    return {"status": "ready"}
