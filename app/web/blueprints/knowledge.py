from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.services.knowledge import KnowledgeService

knowledge_bp = Blueprint("knowledge", __name__)


@knowledge_bp.route("/knowledge")
@login_required
def list_docs():
    svc = KnowledgeService()
    q = request.args.get("q") or ""
    docs = svc.list_documents()
    hits = svc.search(q) if q else []
    return render_template("knowledge/list.html", documents=docs, hits=hits, q=q)


@knowledge_bp.route("/knowledge/<doc_id>")
@login_required
def detail(doc_id: str):
    doc = KnowledgeService().get(doc_id)
    return render_template("knowledge/detail.html", document=doc)


@knowledge_bp.route("/knowledge", methods=["POST"])
@login_required
def create_doc():
    KnowledgeService().create_document(
        title=request.form.get("title") or "未命名文档",
        content=request.form.get("content") or "",
        created_by=current_user.id,
    )
    flash("文档已索引", "ok")
    return redirect(url_for("knowledge.list_docs"))
