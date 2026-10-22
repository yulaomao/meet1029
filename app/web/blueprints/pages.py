from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, send_file, url_for
from flask_login import current_user, login_required

from app.models import ActionItem, Meeting, ScheduledTask
from app.web.auth import handle_login, handle_logout

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/mobile-ca.cer")
def mobile_ca_certificate():
    """Download the public local root certificate for phone setup.

    The certificate is public by design.  Keeping this endpoint unauthenticated
    lets a device install trust before it can sign in over the LAN address.
    """
    path = Path(str(current_app.config.get("TLS_CA_CERT_FILE") or ""))
    if not path.is_file():
        abort(404)
    return send_file(
        path,
        mimetype="application/x-x509-ca-cert",
        as_attachment=True,
        download_name="meeting-ai-lan-root-ca.cer",
    )


@pages_bp.route("/login", methods=["GET", "POST"])
def login():
    return handle_login()


@pages_bp.route("/favicon.ico")
def favicon():
    """Serve the app icon for browsers that request the conventional path."""
    return redirect(url_for("static", filename="icons/icon.svg"))


@pages_bp.route("/logout")
def logout():
    return handle_logout()


@pages_bp.route("/")
@login_required
def dashboard():
    meetings = Meeting.query.order_by(Meeting.created_at.desc()).limit(8).all()
    items = ActionItem.query.order_by(ActionItem.created_at.desc()).limit(8).all()
    tasks = ScheduledTask.query.filter_by(enabled=True).limit(8).all()
    return render_template(
        "dashboard.html",
        meetings=meetings,
        action_items=items,
        scheduled_tasks=tasks,
        user=current_user,
    )


@pages_bp.route("/ai")
@login_required
def ai_workbench():
    return render_template("ai.html")
