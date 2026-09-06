"""Terminal-task feedback: saving a rating never schedules work."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.database import connect, now_iso
from app.services.task_metrics import TERMINAL, get_feedback

router = APIRouter(prefix="/api/tasks", tags=["feedback"])


class TaskFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_changed: StrictBool | None = None
    material_risk_found: StrictBool | None = None
    extra_review_worth_it: StrictBool | None = None
    note: str = Field(default="", max_length=4000)


@router.put("/{task_id}/feedback")
async def save_feedback(task_id: str, body: TaskFeedback):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        if row["status"] not in TERMINAL:
            raise HTTPException(status_code=409, detail="feedback requires a terminal task")
        now = now_iso()
        conn.execute(
            """INSERT INTO task_feedback
               (task_id, decision_changed, material_risk_found, extra_review_worth_it, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET decision_changed=excluded.decision_changed,
               material_risk_found=excluded.material_risk_found, extra_review_worth_it=excluded.extra_review_worth_it,
               note=excluded.note, updated_at=excluded.updated_at""",
            (task_id, body.decision_changed, body.material_risk_found, body.extra_review_worth_it,
             body.note, now, now),
        )
        conn.execute("COMMIT")
    return {"feedback": get_feedback(task_id)}
