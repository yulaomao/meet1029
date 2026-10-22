def test_javascript_modules_use_browser_executable_mime_type(client):
    for path in (
        "/static/js/live.js",
        "/static/js/recorder.js",
        "/static/js/audio_worklet.js",
    ):
        response = client.get(path)

        assert response.status_code == 200
        assert response.mimetype == "text/javascript"


def test_favicon_uses_the_existing_app_icon(client):
    response = client.get("/favicon.ico")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/static/icons/icon.svg")
