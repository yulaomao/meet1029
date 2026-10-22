from __future__ import annotations

from flask import Blueprint, current_app, render_template
from flask_login import login_required

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings")
@login_required
def settings():
    cfg = current_app.config
    providers = current_app.extensions.get("providers")
    return render_template(
        "settings.html",
        speech_backend=cfg.get("SPEECH_BACKEND"),
        llm_backend=cfg.get("LLM_BACKEND"),
        local_model=cfg.get("LOCAL_LLM_MODEL"),
        local_url=cfg.get("LOCAL_LLM_BASE_URL"),
        providers=providers,
    )
