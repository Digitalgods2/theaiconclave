"""Content-free value and compute aggregates."""
from fastapi import APIRouter

from app.services.task_metrics import mode_metrics

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("")
def get_metrics():
    return mode_metrics()
