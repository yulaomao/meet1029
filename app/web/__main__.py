from app.config import get_config
from app.web.extensions import socketio
from app.web.factory import create_app, seed_users
from app.web.tls import socketio_tls_options

app = create_app(get_config())
with app.app_context():
    seed_users()

if __name__ == "__main__":
    import os

    socketio.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        debug=bool(app.config.get("DEBUG")),
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        **socketio_tls_options(app.config),
    )
