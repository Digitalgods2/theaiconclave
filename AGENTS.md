# Repository Guidelines

## Project Structure & Module Organization

This repository contains `ai-switchboard`, a local FastAPI service for governed deliberation between AI coding agents. Core Python code lives in `app/`: `api/` exposes routes, `agents/` and `workers/` handle agent execution, `services/` contains orchestration logic, `protocol/` defines shared contracts, and `utils/` holds support code. Static dashboard assets are in `app/dashboard/`. Tests are in `tests/`. Documentation is in `docs/`, client integrations are in `clients/`, helper scripts are in `tools/`, and the static site is in `landing/`. Runtime SQLite databases, sandboxes, uploads, and exports are written under `data/` and should not be committed.

## Build, Test, and Development Commands

- `pip install -r requirements.txt`: install service, test, export, and HTTP dependencies.
- `cp config.example.yaml config.yaml`: create local configuration; edit ports, adapter paths, and provider settings as needed.
- `uvicorn app.main:app --host 127.0.0.1 --port 8787`: run the local service.
- `uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload`: run with auto-reload during Python development.
- `pytest`: run the full test suite configured by `pytest.ini`.
- `python clients/install.py --check`: inspect installed client integrations.

## Coding Style & Naming Conventions

Use Python 3.13+ and keep code idiomatic, typed where useful, and organized by existing module boundaries. Prefer snake_case for functions, variables, modules, and tests; use PascalCase for classes and Pydantic models. Keep dashboard changes in `app/dashboard/dashboard.js` and `dashboard.css` unless a route or backend contract is required. Avoid broad refactors in unrelated modules.

## Testing Guidelines

Tests use `pytest` with `pytest-asyncio` and are discovered from `tests/`. Name files `test_<feature>.py` and test functions `test_<behavior>()`. Add focused regression tests for API behavior, orchestration flow, config resolution, persistence, and export logic. For local state, assert paths under `data/` or temporary directories rather than user-specific locations.

## Commit & Pull Request Guidelines

Recent history uses concise, imperative subjects such as `Replace landing site contents and add task/asset files` and scoped variants such as `help.html: refresh for DR0015-0018 + launcher + widened search`. Keep commits focused and mention the subsystem when helpful. Pull requests should include a summary, test results, linked issue or decision record when applicable, and screenshots for dashboard or landing-page changes.

## Security & Configuration Tips

Do not commit `config.yaml`, secrets, runtime databases, sandboxes, or exported task data. Use `config.example.yaml` for documented defaults. Keep the service bound to `127.0.0.1` unless a change explicitly requires broader network exposure.
