"""Terminology management for local FunASR hotwords and Qwen correction context."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import Terminology
from app.security.rbac import can
from app.services.audit import AuditService
from app.web.extensions import db

terminology_bp = Blueprint("terminology", __name__)


def _aliases(raw: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (raw or "").replace("，", ",").split(",") if x.strip()))


@terminology_bp.route("/terminology", methods=["GET", "POST"])
@login_required
def list_terms():
    q = (request.args.get("q") or "").strip()
    rows = Terminology.query.order_by(Terminology.priority.desc(), Terminology.term.asc()).all()
    if q:
        rows = [row for row in rows if q.lower() in row.term.lower() or any(q.lower() in x.lower() for x in row.aliases or [])]
    if request.method == "POST":
        if not can(current_user, "manage_terminology"):
            flash("没有维护术语库的权限", "error")
            return redirect(url_for("terminology.list_terms"))
        term = (request.form.get("term") or "").strip()
        if not term:
            flash("术语不能为空", "error")
            return render_template("terminology/list.html", terms=rows, q=q)
        row = Terminology.query.filter_by(term=term).first()
        if row is None:
            row = Terminology(term=term)
            db.session.add(row)
        row.aliases = _aliases(request.form.get("aliases") or "")
        row.description = (request.form.get("description") or "").strip()
        row.priority = int(request.form.get("priority") or 0)
        row.enabled = request.form.get("enabled") in {"on", "1", "true"}
        db.session.commit()
        AuditService().record("terminology_upsert", current_user.id, target=row.id, extra={"term": term})
        flash("术语已保存，下一段转写将自动使用", "ok")
        return redirect(url_for("terminology.list_terms"))
    return render_template("terminology/list.html", terms=rows, q=q)


@terminology_bp.route("/terminology/<term_id>/toggle", methods=["POST"])
@login_required
def toggle_term(term_id: str):
    if not can(current_user, "manage_terminology"):
        flash("没有维护术语库的权限", "error")
        return redirect(url_for("terminology.list_terms"))
    row = db.session.get(Terminology, term_id)
    if row is None:
        flash("术语不存在", "error")
        return redirect(url_for("terminology.list_terms"))
    row.enabled = not row.enabled
    db.session.commit()
    AuditService().record("terminology_toggle", current_user.id, target=row.id, extra={"enabled": row.enabled})
    return redirect(url_for("terminology.list_terms"))
