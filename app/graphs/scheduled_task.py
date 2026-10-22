"""Scheduled-task agent tools + graph."""

from __future__ import annotations

from app.models import ActionItem, Meeting, Notification
from app.services.knowledge import KnowledgeService
from app.web.extensions import db


ALLOWED_TOOLS = {
    "search_knowledge",
    "list_meetings",
    "create_action_item",
    "send_notification",
    "generate_report",
    "search_meetings",
    "get_meeting",
    "search_transcripts",
    "search_action_items",
    "read_file",
    "write_file",
    "create_report",
    "http_request",
}

DENIED_TOOLS = {"execute_shell", "delete_file", "send_message", "modify_system"}


def run_tool(name: str, arguments: dict, *, allowed: list[str] | None = None) -> dict:
    if name in DENIED_TOOLS:
        return {"error": "permission denied", "tool": name}
    permit = set(allowed or ALLOWED_TOOLS)
    # A task's permission list is a real allow-list.  The old ``and`` condition
    # accidentally granted every globally-known tool even when omitted here.
    if name not in permit:
        return {"error": "permission denied", "tool": name}
    if name in {"search_knowledge"}:
        hits = KnowledgeService().search(arguments.get("query") or "", k=int(arguments.get("k") or 5))
        return {"hits": hits}
    if name in {"list_meetings", "search_meetings"}:
        rows = Meeting.query.order_by(Meeting.created_at.desc()).limit(20).all()
        return {"meetings": [{"id": m.id, "name": m.name, "status": m.status} for m in rows]}
    if name == "get_meeting":
        m = db.session.get(Meeting, arguments.get("meeting_id"))
        if not m:
            return {"error": "not found"}
        return {"id": m.id, "name": m.name, "status": m.status}
    if name == "create_action_item":
        item = ActionItem(
            description=arguments.get("description") or "",
            owner=arguments.get("owner") or "",
            meeting_id=arguments.get("meeting_id"),
        )
        db.session.add(item)
        db.session.commit()
        return {"id": item.id}
    if name == "send_notification":
        n = Notification(
            user_id=arguments.get("recipient"),
            title=arguments.get("title") or "通知",
            content=arguments.get("content") or "",
        )
        db.session.add(n)
        db.session.commit()
        return {"id": n.id}
    if name in {"generate_report", "create_report"}:
        return {"report": arguments.get("title") or "报告", "body": arguments.get("body") or ""}
    if name == "search_action_items":
        rows = ActionItem.query.limit(50).all()
        return {"items": [{"id": a.id, "description": a.description, "status": a.status} for a in rows]}
    return {"error": "unknown tool", "tool": name}


def run_scheduled_graph(task, run, llm) -> dict:
    prompt = task.prompt or ""
    steps = []
    if "知识" in prompt or "search" in prompt.lower():
        steps.append(("search_knowledge", {"query": prompt}))
    steps.append(("list_meetings", {}))
    results = []
    for name, args in steps:
        results.append({"tool": name, "result": run_tool(name, args, allowed=task.tool_permissions)})
    answer = llm.complete(f"根据工具结果完成任务：{prompt}\n{results}", json_mode=True)
    return {"answer": answer, "tools": results}
