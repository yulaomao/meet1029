from __future__ import annotations

from flask import Blueprint, current_app, render_template
from flask_login import login_required

from app.services.health import HealthService

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
@login_required
def health_page():
    report = HealthService(current_app.extensions.get("providers")).check()
    return render_template("health.html", health=report)
