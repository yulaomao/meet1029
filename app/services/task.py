"""Scheduled AI tasks + APScheduler cron support.

execution_key = task_id + scheduled_fire_time  (idempotent)
"""

from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from app.models import ScheduledTask, TaskRun
from app.models.enums import TaskRunStatus
from app.web.extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def execution_key(task_id: str, fire_time: datetime) -> str:
    return f"{task_id}:{fire_time.isoformat()}"


class TaskService:
    def create(
        self,
        name: str,
        prompt: str,
        *,
        schedule_type: str = "cron",
        cron_expression: str = "0 9 * * *",
        created_by: str | None = None,
        enabled: bool = True,
        tool_permissions: list | None = None,
    ) -> ScheduledTask:
        task = ScheduledTask(
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            created_by=created_by,
            enabled=enabled,
            tool_permissions=tool_permissions or ["search_knowledge", "list_meetings", "create_action_item"],
        )
        db.session.add(task)
        db.session.commit()
        return task

    def list_tasks(self) -> list[ScheduledTask]:
        return ScheduledTask.query.order_by(ScheduledTask.created_at.desc()).all()

    def get(self, task_id: str) -> ScheduledTask | None:
        return db.session.get(ScheduledTask, task_id)

    def next_fire(self, task: ScheduledTask, now: datetime | None = None) -> datetime | None:
        now = now or utcnow()
        if task.schedule_type == "once":
            return task.run_at
        expr = task.cron_expression or "0 9 * * *"
        if task.schedule_type == "daily":
            expr = "0 9 * * *"
        if task.schedule_type == "weekly":
            expr = "0 9 * * 1"
        itr = croniter(expr, now)
        return itr.get_next(datetime)

    def run_once(self, task: ScheduledTask, fire_time: datetime | None = None, runner=None) -> TaskRun:
        fire_time = fire_time or utcnow()
        key = execution_key(task.id, fire_time)
        existing = TaskRun.query.filter_by(execution_key=key).first()
        if existing:
            return existing
        run = TaskRun(
            task_id=task.id,
            execution_key=key,
            started_at=utcnow(),
            status=TaskRunStatus.RUNNING.value,
            input={"prompt": task.prompt},
        )
        db.session.add(run)
        db.session.commit()
        try:
            output = {"result": "ok"}
            if runner:
                output = runner(task, run) or output
            run.output = output
            run.status = TaskRunStatus.SUCCESS.value
            run.finished_at = utcnow()
        except Exception as exc:
            run.status = TaskRunStatus.FAILED.value
            run.error = str(exc)
            run.finished_at = utcnow()
        db.session.commit()
        return run


class SchedulerController:
    """Thin APScheduler wrapper used by the task worker and unit tests."""

    def __init__(self, task_service: TaskService | None = None):
        self.task_service = task_service or TaskService()
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._started = False

    def add_cron(self, task: ScheduledTask, func, job_id: str | None = None) -> str:
        jid = job_id or f"task-{task.id}"
        expr = task.cron_expression or "0 9 * * *"
        trigger = CronTrigger.from_crontab(expr, timezone="Asia/Shanghai")
        self.scheduler.add_job(func, trigger=trigger, id=jid, replace_existing=True, args=[task.id])
        return jid

    def start(self) -> None:
        if not self._started:
            self.scheduler.start(paused=False)
            self._started = True

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    def job_ids(self) -> list[str]:
        return [j.id for j in self.scheduler.get_jobs()]
