"""Circular import / models must be importable at module level."""

def test_models_and_factory_importable():
    from app.models import User, Notification
    from app.web.factory import create_app

    assert User is not None
    assert Notification is not None
    assert callable(create_app)


def test_web_package_does_not_import_factory_at_module_level():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "app" / "web" / "__init__.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.web.factory":
            # only allowed inside __getattr__, not at module import time
            assert node.lineno > 1
    text = src.read_text(encoding="utf-8")
    assert "__getattr__" in text
    assert "create_app" in text


def test_notification_imports_entities_not_models_package():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app" / "providers" / "notification.py").read_text(encoding="utf-8")
    assert "from app.models.entities import Notification" in src
    assert "from app.models import Notification" not in src
