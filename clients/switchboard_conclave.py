"""Portable command-line client for The AI Conclave Switchboard.

This module intentionally uses only the Python standard library so Claude Code,
Codex, and Gemini can share one installed command without a repository path or
user-specific helper location.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:8787"
_INVOKED_BY = "claude-code"


def endpoint() -> str:
    return os.environ.get("SWITCHBOARD_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _pop_invoked_by(argv: list[str]) -> str:
    for i, value in enumerate(argv):
        if value == "--invoked-by" and i + 1 < len(argv):
            chosen = argv[i + 1]
            del argv[i:i + 2]
            return chosen
        if value.startswith("--invoked-by="):
            del argv[i]
            return value.split("=", 1)[1]
    return os.environ.get("SWITCHBOARD_INVOKED_BY", "claude-code")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = os.environ.get("CONCLAVE_API_TOKEN")
    if token:
        headers["X-Conclave-Token"] = token
    req = urllib.request.Request(
        endpoint() + path, data=data, method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def _get(path: str) -> dict:
    return _request("GET", path)


def _post(path: str, body: dict) -> dict:
    return _request("POST", path, body)


def health() -> bool:
    try:
        return _get("/api/health").get("status") == "ok"
    except (OSError, urllib.error.URLError):
        return False


def submit(mode: str, agents: list[str], question: str,
           parent_task_id: str | None = None, inherited: dict | None = None) -> str:
    if mode == "conclave":
        primary, consultants = None, agents
    elif mode == "consult":
        if len(agents) < 2:
            raise ValueError("consult mode needs at least 2 agents (primary + consultant)")
        primary, consultants = agents[0], agents[1:]
    elif mode == "resolve":
        primary, consultants = agents[0], agents[1:]
    else:
        raise ValueError(f"unknown mode: {mode}")
    payload = {
        "protocol_version": "1.2", "source": "cli", "source_agent": _INVOKED_BY,
        "mode": mode, "task_type": "general_consultation", "user_request": question,
        "primary_agent": primary, "consultants": consultants,
        "context": {"files": [], "error": None, "git_diff": None, "extra": {}},
        "permissions": {"can_read_files": True, "can_write_files": False,
                         "can_run_commands": False, "can_access_network": False,
                         "can_install_packages": False, "can_apply_patches": False,
                         "can_read_env_files": False, "can_read_secrets": False},
        # Time is advisory. The dashboard warns at this threshold and the user
        # can explicitly abort; the service is responsible for round guards.
        "limits": {"max_rounds": 5, "notify_after_seconds": 360,
                    "max_context_tokens": None, "convergence_threshold": 1.0},
        "parent_task_id": parent_task_id,
    }
    if inherited:
        for key in ("project_path", "decision_project_id", "judge_agent", "synthesis_agent", "permissions", "limits"):
            if key in inherited:
                payload[key] = inherited[key]
        context = inherited.get("context") or {}
        extra = context.get("extra") or {}
        payload["context"] = {"files": context.get("files", []), "extra": {
            key: extra[key] for key in ("include_sandbox", "attachments", "evidence_snapshot_ids") if key in extra
        }}
    response = _post("/api/tasks", payload)
    return response["task_id"]


def _usage(envelope: dict) -> tuple[int, int, float]:
    input_tokens = output_tokens = 0
    cost = 0.0
    for run in envelope.get("runs", []):
        input_tokens += int(run.get("input_tokens") or 0)
        output_tokens += int(run.get("output_tokens") or 0)
        cost += float(run.get("cost_usd") or 0)
    return input_tokens, output_tokens, cost


def wait_for(task_id: str, notify_after_seconds: int | None = None) -> dict:
    """Poll indefinitely until terminal/input, reporting elapsed cost telemetry."""
    started = time.monotonic()
    next_report = notify_after_seconds or 60
    while True:
        envelope = _get(f"/api/tasks/{urllib.parse.quote(task_id)}")
        status = envelope.get("task", {}).get("status")
        if status in {"completed", "failed", "cancelled", "awaiting_user_input"}:
            return envelope
        elapsed = int(time.monotonic() - started)
        if elapsed >= next_report:
            inp, out, cost = _usage(envelope)
            spend = f", estimated cost ${cost:.4f}" if cost else ""
            print(f"Still working ({elapsed}s elapsed; {inp + out:,} tokens{spend}). "
                  f"Abort with: python -m switchboard_conclave abort {task_id}",
                  file=sys.stderr)
            next_report += 60
        time.sleep(5)


def resolve_task_id(value: str) -> str:
    if value.lower() != "latest":
        return value
    tasks = _get("/api/tasks?limit=1").get("tasks", [])
    if not tasks:
        raise RuntimeError("no tasks exist yet")
    return tasks[0]["id"]


def get_task(task_id: str) -> dict:
    return _get(f"/api/tasks/{urllib.parse.quote(task_id)}")


def render(envelope: dict) -> None:
    task = envelope["task"]
    print(f"task_id   : {task['id']}\nstatus    : {task['status']}\nmode      : {task['mode']}")
    print(f"primary   : {task.get('primary_agent')}\nconsultants: {task.get('consultants')}")
    if task.get("error_message"):
        print(f"error     : {task['error_message']}")
    for message in envelope.get("messages", []):
        structured = message.get("structured") or {}
        print(f"\n-- {message.get('agent_name')} ({message.get('role')}) --")
        for key, label, limit in (("summary", "summary", 400), ("position", "position", 600),
                                  ("critique", "critique", 600), ("analysis", "analysis", 800)):
            if structured.get(key):
                print(f"   {label}: {_truncate(structured[key], limit)}")
        if message.get("message_type") == "user_input_request":
            print(f"   QUESTION: {message.get('content', '')}")
    result = envelope.get("final_result")
    if result:
        print("\n=== Final Result ===\n" + str(result.get("final_answer", "")))
    if task["status"] == "awaiting_user_input":
        print(f"\n>>> Answer with: python -m switchboard_conclave answer {task['id']} \"<answer>\"")


def _truncate(value: object, length: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= length else text[:length].rstrip() + "..."


def thread(task_id: str) -> dict:
    return _get(f"/api/tasks/{urllib.parse.quote(task_id)}/thread")


def continue_thread(parent: str, question: str) -> str:
    task = get_task(parent)["task"]
    primary = task.get("primary_agent")
    consultants = task.get("consultants", [])
    agents = consultants if task["mode"] == "conclave" else [primary] + consultants
    return submit(task["mode"], [a for a in agents if a], question, parent, inherited=task)


def main() -> None:
    global _INVOKED_BY
    argv = sys.argv[1:]
    _INVOKED_BY = _pop_invoked_by(argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__, file=sys.stderr)
        return
    command = argv.pop(0)
    if command == "health":
        print("ok" if health() else "down")
        return
    if not health():
        print(f"Switchboard service is not responding at {endpoint()}", file=sys.stderr)
        raise SystemExit(3)
    if command in {"run", "submit"} and len(argv) >= 3:
        tid = submit(argv[0], [a.strip() for a in argv[1].split(",") if a.strip()], argv[2])
        if command == "submit": print(tid); return
        print(f"task_id: {tid}", file=sys.stderr); render(wait_for(tid)); return
    if command == "wait" and argv:
        render(wait_for(resolve_task_id(argv[0]))); return
    if command == "abort" and argv:
        tid = resolve_task_id(argv[0]); print(json.dumps(_post(f"/api/tasks/{tid}/cancel", {}), indent=2)); return
    if command == "answer" and len(argv) >= 2:
        text = sys.stdin.read() if argv[1] == "-" else argv[1]
        print(json.dumps(_post(f"/api/tasks/{resolve_task_id(argv[0])}/answer", {"answer": text}), indent=2)); return
    if command == "decide" and len(argv) >= 2:
        response = _post(f"/api/tasks/{resolve_task_id(argv[0])}/decide", {"decision": argv[1]})
        print(response.get("user_decision", "")); return
    if command == "decision" and argv:
        render(get_task(resolve_task_id(argv[0]))); return
    if command == "continue" and len(argv) >= 2:
        tid = continue_thread(resolve_task_id(argv[0]), argv[1]); print(f"task_id: {tid}", file=sys.stderr); render(wait_for(tid)); return
    if command == "thread" and argv:
        print(json.dumps(thread(resolve_task_id(argv[0])), indent=2)); return
    print(__doc__, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
