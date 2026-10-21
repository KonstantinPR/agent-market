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
    import threading

    from app.database import ensure_schema

    ensure_schema(seed=False)

    # Прогрев индекса фото в фоне: первый запрос раздела «Фото для маркетплейса»
    # иначе строит индекс на месте (обход C:\YandexDisk\ФОТОГРАФИИ ~3 с, а при
    # залипании Яндекс.Диска — сильно дольше). Нить demon, старт не блокирует.
    def _warm_photos():
        try:
            from app.services import photos as photos_service
            photos_service.get_index()._get()  # noqa: SLF001
        except Exception:  # noqa: BLE001 — прогрев не должен ронять старт
            pass

    threading.Thread(target=_warm_photos, daemon=True).start()
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
    import os

    import uvicorn

    from app.config import settings
    from app.database import ensure_schema

    ensure_schema()
    if os.environ.get("AGENT_MARKET_NO_RELOAD") == "1":
        uvicorn.run(app, host=settings.app_host, port=settings.app_port)
    else:
        # Авто-перезапуск при правках бэкенда (app/) — изменения видны сразу,
        # без ручного рестарта. Отключается через AGENT_MARKET_NO_RELOAD=1.
        uvicorn.run(
            "app.main:app",
            host=settings.app_host,
            port=settings.app_port,
            reload=True,
            reload_dirs=[str(BASE_DIR / "app")],
        )