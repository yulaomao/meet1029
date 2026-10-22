"""Flask extensions.

Socket.IO uses async_mode=threading (not eventlet/gevent).
"""

from __future__ import annotations

from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
socketio = SocketIO(async_mode="threading", cors_allowed_origins="*")
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "pages.login"
login_manager.login_message = "请先登录"
