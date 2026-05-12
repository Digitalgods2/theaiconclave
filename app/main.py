"""FastAPI app entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import agents as agents_api
from app.api import git as git_api
from app.api import health as health_api
from app.api import settings as settings_api
from app.api import tasks as tasks_api
from app.api import uploads as uploads_api
from app.config import load_config
from app.database import init_database
from app.services import agent_registry
from app.services.retention import retention_loop
from app.workers.task_worker import worker_loop

config = load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("switchboard")

    init_database(config.database.path)
    agent_registry.clear()
    agent_registry.init_registry()
    agent_registry.register_ollama_cloud_models(config)

    # Sweep orphan sandboxes left over from crashed/aborted tasks.
    from app.database import connect
    from app.services.sandbox import sweep_orphan_sandboxes
    with connect() as conn:
        active = {
            row["id"] for row in conn.execute(
                "SELECT id FROM tasks WHERE status IN ('pending','running','awaiting_user_input','waiting_for_user')"
            ).fetchall()
        }
    sweep_orphan_sandboxes(active)

    worker_task = asyncio.create_task(worker_loop(config))
    retention_task = asyncio.create_task(retention_loop(config))
    logger.info("Switchboard service started on %s:%d", config.server.host, config.server.port)

    try:
        yield
    finally:
        logger.info("Shutting down.")
        for t in (worker_task, retention_task):
            t.cancel()
        for t in (worker_task, retention_task):
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="AI Switchboard",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health_api.router)
app.include_router(tasks_api.router)
app.include_router(agents_api.router)
app.include_router(uploads_api.router)
app.include_router(git_api.router)
app.include_router(settings_api.router)

_DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
app.mount("/static", StaticFiles(directory=str(_DASHBOARD_DIR)), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(_DASHBOARD_DIR / "index.html")
