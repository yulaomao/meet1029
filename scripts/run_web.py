"""Start the Flask-SocketIO web process (threading mode)."""
import os

from app.config import get_config
from app.web.extensions import socketio
from app.web.factory import create_app, seed_users
from app.web.tls import socketio_tls_options

app = create_app(get_config())
with app.app_context():
    seed_users()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(
        app,
        host=host,
        port=port,
        allow_unsafe_werkzeug=True,
        use_reloader=False,
        **socketio_tls_options(app.config),
    )
