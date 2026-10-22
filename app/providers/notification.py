"""In-app DB notifications. Feishu/WeCom/Email are empty stubs for later."""

from __future__ import annotations

from app.models.entities import Notification
from app.providers.base import NotificationProvider
from app.web.extensions import db


class InAppNotificationProvider(NotificationProvider):
    def send(self, recipient: str, title: str, content: str) -> None:
        row = Notification(user_id=recipient or None, title=title, content=content)
        db.session.add(row)
        db.session.commit()


class FeishuProvider(NotificationProvider):
    def send(self, recipient: str, title: str, content: str) -> None:
        return None


class WeComProvider(NotificationProvider):
    def send(self, recipient: str, title: str, content: str) -> None:
        return None


class EmailProvider(NotificationProvider):
    def send(self, recipient: str, title: str, content: str) -> None:
        return None
