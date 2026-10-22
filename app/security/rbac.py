"""Role-based access control. Roles: Admin / Member / Viewer."""

from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user

from app.exceptions import PermissionDenied
from app.models.enums import RoleName

START_MEETING_ROLES = {RoleName.ADMIN.value, RoleName.MEMBER.value}
MANAGE_MEMBERS_ROLES = {RoleName.ADMIN.value}
DELETE_MEETING_ROLES = {RoleName.ADMIN.value}
SETTINGS_ROLES = {RoleName.ADMIN.value}
CREATE_TASK_ROLES = {RoleName.ADMIN.value, RoleName.MEMBER.value}
CREATE_KNOWLEDGE_ROLES = {RoleName.ADMIN.value, RoleName.MEMBER.value}


def role_of(user) -> str:
    if user is None:
        return ""
    return getattr(user, "role", "") or ""


def can(user, action: str) -> bool:
    role = role_of(user)
    mapping = {
        "start_meeting": START_MEETING_ROLES,
        "create_meeting": START_MEETING_ROLES,
        "manage_members": MANAGE_MEMBERS_ROLES,
        "delete_meeting": DELETE_MEETING_ROLES,
        "settings": SETTINGS_ROLES,
        "create_task": CREATE_TASK_ROLES,
        "create_knowledge": CREATE_KNOWLEDGE_ROLES,
        "manage_terminology": CREATE_KNOWLEDGE_ROLES,
        "enroll_speaker": START_MEETING_ROLES,
        "view": {RoleName.ADMIN.value, RoleName.MEMBER.value, RoleName.VIEWER.value},
    }
    allowed = mapping.get(action, START_MEETING_ROLES)
    return role in allowed


def require(user, action: str) -> None:
    if not can(user, action):
        raise PermissionDenied(action, role_of(user) or "anonymous")


def login_required_role(*roles: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
