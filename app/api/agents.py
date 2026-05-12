"""Agent listing and connection test endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services import agent_registry

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(include_internal: bool = False) -> dict:
    """List registered agents. Hides adapters marked `internal=True` (e.g. fake) by default."""
    names = agent_registry.list_names()
    if include_internal:
        return {"agents": names}
    public = [n for n in names if not agent_registry.get(n).internal]
    return {"agents": public}


@router.post("/{agent_name}/test")
async def test_agent(agent_name: str) -> dict:
    try:
        adapter = agent_registry.get(agent_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"agent {agent_name} not registered")
    result = await adapter.test_connection()
    return result.model_dump(mode="json")
