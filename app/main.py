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
from app.api import help as help_api
from app.api import settings as settings_api
from app.api import tasks as tasks_api
from app.api import uploads as uploads_api
from app.config import get_config
from app.database import init_database
from app.services import agent_registry
from app.services import migration
from app.services import pidlock
from app.services.retention import retention_loop
from app.utils.paths import default_db_path, platform_user_data_root, user_data_root
from app.workers.task_worker import worker_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Step 1 — basic logging early so migration/startup messages have a destination.
    # The full level is reapplied after config loads (which might raise the level).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("switchboard")

    # Step 1.5 — rebrand: a packaged build whose data still lives under the
    # pre-rebrand directory name ("AI Switchboard") is relocated to the current
    # name ("The AI Conclave") before anything opens the root. The guard limits
    # this to a genuine platform-resolved launch — it is skipped in dev mode and
    # under a SWITCHBOARD_DATA_DIR override (where user_data_root() differs from
    # the platform path). Idempotent once migrated.
    try:
        if user_data_root() == platform_user_data_root():
            renamed = migration.migrate_legacy_data_dir()
            if renamed:
                logger.info("Rebrand data-directory migration completed: %s", renamed)
    except migration.MigrationBlocked as e:
        logger.error("Refusing to start — rebrand data-directory migration blocked.\n%s", e)
        raise SystemExit(2) from e

    # Step 2 — FIRST AWAITABLE per DR0016: run the first-run migration before
    # any writer (init_database, retention worker, orphan reaper, etc.) opens
    # the destination root. Migration is a no-op in dev mode and on every
    # subsequent launch.
    try:
        result = migration.maybe_migrate()
        if result:
            logger.info("Migration completed: %s", result)
    except migration.MigrationBlocked as e:
        logger.error("Refusing to start — migration blocked.\n%s", e)
        raise SystemExit(2) from e

    # Step 3 — resolve config (lazy), reapply logging level.
    config = get_config()
    logging.getLogger().setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))

    # Step 4 — single-instance enforcement. Pidlock lives in user_data_root()
    # (DR0016: decoupled from config.database.path's parent — the wrong primitive).
    data_root = user_data_root()
    try:
        lock_path = pidlock.acquire(data_root)
    except pidlock.PidLockBusy as e:
        logger.error("Refusing to start — another AI Conclave Switchboard is running.\n%s", e)
        raise SystemExit(2) from e

    # Step 5 — initialize the DB. config.database.path = None means "use
    # default_db_path()" per DR0016. An explicit string in config.yaml still wins.
    db_path = Path(config.database.path) if config.database.path else default_db_path()
    init_database(db_path)

    help_api.sync_help_metadata_from_file()
    agent_registry.clear()
    agent_registry.init_registry(config)
    agent_registry.register_openrouter_models(config)

    # Reap any tasks left in `running` from a previous crashed worker.
    # See app/services/orphan_reaper.py — Phase 1 of post-DR plan
    # (tsk_01KRSW6AS3M66B4RRJE3JFAPRV). No-op when there are no orphans.
    from app.services.orphan_reaper import reap_orphans
    try:
        reap_orphans()
    except Exception as e:  # noqa: BLE001 — never block startup on reaper failure
        logger.warning("orphan reaper failed: %s", e)

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
    logger.info("AI Conclave Switchboard service started on %s:%d", config.server.host, config.server.port)

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
        pidlock.release(lock_path)


app = FastAPI(
    title="The AI Conclave Switchboard",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health_api.router)
app.include_router(tasks_api.router)
app.include_router(tasks_api.trajectories_router)
app.include_router(agents_api.router)
app.include_router(uploads_api.router)
app.include_router(git_api.router)
app.include_router(settings_api.router)
app.include_router(help_api.router)

_DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
app.mount("/static", StaticFiles(directory=str(_DASHBOARD_DIR)), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(_DASHBOARD_DIR / "index.html")
