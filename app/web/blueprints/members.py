from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import SpeakerProfile, User
from app.services.enroll import EnrollError, enroll_profile
from app.web.extensions import db

members_bp = Blueprint("members", __name__)


@members_bp.route("/team")
@login_required
def team():
    users = User.query.order_by(User.created_at.asc()).all()
    profiles = {p.user_id: p for p in SpeakerProfile.query.all()}
    return render_template("team.html", users=users, profiles=profiles)


def _ensure_profile(user: User) -> SpeakerProfile:
    profile = SpeakerProfile.query.filter_by(user_id=user.id).first()
    if not profile:
        profile = SpeakerProfile(user_id=user.id, display_name=user.name, embedding=[])
        db.session.add(profile)
        db.session.commit()
    return profile


@members_bp.route("/team/<user_id>/enroll", methods=["GET", "POST"])
@login_required
def enroll(user_id: str):
    user = db.session.get(User, user_id) or current_user
    profile = _ensure_profile(user)
    if request.method == "POST":
        audio = request.files.get("audio") or request.files.get("file")
        data = audio.read() if audio else b""
        try:
            seconds = request.form.get("sample_seconds")
            enroll_profile(
                profile,
                data,
                sample_seconds=float(seconds) if seconds not in (None, "") else None,
                embedding_provider=current_app.extensions["providers"].embedding,
            )
        except EnrollError as exc:
            flash(exc.message, "error")
            return render_template("enroll.html", user=user, profile=profile)
        flash("声纹已登记", "ok")
        return redirect(url_for("members.team"))
    return render_template("enroll.html", user=user, profile=profile)
