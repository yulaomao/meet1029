from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import ActionItem, ScheduledTask, TaskRun
from app.services.task import TaskService
from app.security.rbac import can
from app.web.extensions import db

tasks_bp = Blueprint("tasks", __name__)


@tasks_bp.route("/tasks")
@login_required
def list_tasks():
    items = ActionItem.query.order_by(ActionItem.created_at.desc()).all()
    scheduled = TaskService().list_tasks()
    return render_template("tasks/list.html", action_items=items, scheduled_tasks=scheduled)


@tasks_bp.route("/tasks/<task_id>")
@login_required
def task_detail(task_id: str):
    scheduled = TaskService().get(task_id)
    if scheduled:
        runs = TaskRun.query.filter_by(task_id=scheduled.id).order_by(TaskRun.started_at.desc()).all()
        return render_template("tasks/detail.html", task=scheduled, runs=runs, action_item=None)
    item = ActionItem.query.filter_by(id=task_id).first()
    return render_template("tasks/detail.html", task=None, runs=[], action_item=item)


@tasks_bp.route("/tasks/new", methods=["POST"])
@login_required
def create_scheduled():
    if not can(current_user, "create_task"):
        flash("没有创建任务的权限", "error")
        return redirect(url_for("tasks.list_tasks"))
    TaskService().create(
        name=request.form.get("name") or "未命名任务",
        prompt=request.form.get("prompt") or "",
        cron_expression=request.form.get("cron_expression") or "0 9 * * *",
        created_by=current_user.id,
    )
    flash("已创建定时任务", "ok")
    return redirect(url_for("tasks.list_tasks"))


@tasks_bp.route("/tasks/<task_id>/toggle", methods=["POST"])
@login_required
def toggle_scheduled(task_id: str):
    task = TaskService().get(task_id)
    if not task:
        flash("任务不存在", "error")
    elif not can(current_user, "create_task"):
        flash("没有管理任务的权限", "error")
    else:
        task.enabled = not task.enabled
        db.session.commit()
        flash("任务已启用" if task.enabled else "任务已暂停", "ok")
    return redirect(url_for("tasks.task_detail", task_id=task_id))


@tasks_bp.route("/tasks/<task_id>/run", methods=["POST"])
@login_required
def run_scheduled(task_id: str):
    task = TaskService().get(task_id)
    if not task:
        flash("任务不存在", "error")
    elif not can(current_user, "create_task"):
        flash("没有执行任务的权限", "error")
    else:
        from app.graphs.scheduled_task import run_scheduled_graph

        TaskService().run_once(task, runner=lambda t, r: run_scheduled_graph(t, r, current_app.extensions["providers"].llm))
        flash("任务已执行", "ok")
    return redirect(url_for("tasks.task_detail", task_id=task_id))
