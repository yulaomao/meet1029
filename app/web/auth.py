from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_user, logout_user
from werkzeug.security import check_password_hash

from app.models import User
from app.web.extensions import db, login_manager


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, user_id)


def authenticate(email: str, password: str) -> User | None:
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password) and user.is_active:
        return user
    return user if user and check_password_hash(user.password_hash, password) else None


def handle_login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            nxt = request.args.get("next") or url_for("pages.dashboard")
            return redirect(nxt)
        flash("邮箱或密码错误", "error")
    return render_template("login.html")


def handle_logout():
    logout_user()
    return redirect(url_for("pages.login"))
