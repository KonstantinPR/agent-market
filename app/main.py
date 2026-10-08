import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import router
from app.config import BASE_DIR
from app.services import progress as progress_service

STATIC_DIR = BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(_app):
    from app.database import ensure_schema

    ensure_schema(seed=False)
    yield


app = FastAPI(title="Agent Market", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def progress_op_middleware(request, call_next):
    """Прокидывает op-id выкачки (X-Progress-Id) в ContextVar на время запроса.

    Контекст anyio передаёт его и в sync-эндпоинты (threadpool), откуда его
    читает services/progress.report() в циках пагинации провайдеров.
    """
    progress_service.set_op(request.headers.get(progress_service.HEADER, ""))
    try:
        return await call_next(request)
    finally:
        progress_service.set_op("")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    from app.config import settings
    from app.database import ensure_schema

    ensure_schema()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)