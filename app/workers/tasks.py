"""Task worker process with APScheduler."""

from __future__ import annotations

import time

from app.config import get_config
from app.graphs.scheduled_task import run_scheduled_graph
from app.logging import configure_logging, get_logger
from app.models import ScheduledTask
from app.services.task import SchedulerController, TaskService
from app.web.factory import create_app


def main() -> None:
    configure_logging()
    log = get_logger("task-worker")
    app = create_app(get_config())
    svc = TaskService()
    ctrl = SchedulerController(svc)

    def fire(task_id: str) -> None:
        with app.app_context():
            task = svc.get(task_id)
            if not task or not task.enabled:
                return
            llm = app.extensions["providers"].llm
            svc.run_once(task, runner=lambda t, r: run_scheduled_graph(t, r, llm))

    with app.app_context():
        for task in ScheduledTask.query.filter_by(enabled=True).all():
            if task.cron_expression or task.schedule_type in {"cron", "daily", "weekly"}:
                ctrl.add_cron(task, fire)
        ctrl.start()
        log.info("task worker started jobs=%s", ctrl.job_ids())
        try:
            while True:
                time.sleep(5)
        finally:
            ctrl.shutdown()


if __name__ == "__main__":
    main()
