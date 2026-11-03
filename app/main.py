"""FastAPI 应用装配 + 启动入口。

`python -m app.main` 起服务；静态前端直接挂在 `/`。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, APP_SLUG, __version__, db, jobs
from .api import router
from .config import WEB_DIR, cfg, ensure_dirs


def create_app() -> FastAPI:
    ensure_dirs()
    db.init_db()
    jobs.start()

    app = FastAPI(title=f"{APP_NAME} ({APP_SLUG})", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = str(cfg.get("server.host", "127.0.0.1"))
    port = int(cfg.get("server.port", 8787))
    print(f"{APP_SLUG}（{APP_NAME}） →  http://{host}:{port}")
    if not cfg.proxy:
        print("提示：没配 proxy。国内网络下 YouTube 会连不上，去 config.toml 填 [network] proxy")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
