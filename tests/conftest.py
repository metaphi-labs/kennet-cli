import pytest


@pytest.fixture(autouse=True)
def _no_release_look(monkeypatch):
    """No test reaches GitHub's releases before a profile run; tests/test_update.py drives that look itself."""
    monkeypatch.setenv("KENNET_UPDATED", "test")


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """No test opens a browser tab; a test that checks what was opened patches this itself."""
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: True)


class Browser:
    """The person's browser: opening the approval page sends it back to the terminal's own listener (the device
    code's `redirect_uri`) with `answer`, the approval's code or a no, as the page does; None approves nothing.
    `sent_on` is where the listener passed the browser on to."""

    def __init__(self) -> None:
        self.answer: dict | None = {"code": "claim"}
        self.opened: list[str] = []
        self.sent_on: list[str] = []
        self.back: str | None = None
        self.going: list = []

    def settle(self) -> None:
        """Wait for the browser to have read the listener's answer."""
        for t in self.going:
            t.join(5)

    def code(self, body: dict):
        """The stand-in for accounts' `/device/code`: `body`, and the return address the terminal named, kept."""
        from urllib.parse import parse_qs

        import httpx

        def answer(request):
            self.back = parse_qs(request.content.decode()).get("redirect_uri", [None])[0]
            return httpx.Response(200, json=body)
        return answer

    def open(self, url, *a, **k):
        import threading
        import urllib.error
        import urllib.request
        from urllib.parse import urlencode
        self.opened.append(url)
        if self.answer is None or self.back is None:
            return True
        to = f"{self.back}?{urlencode(self.answer)}"

        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        def go():
            try:
                urllib.request.build_opener(Stay).open(to, timeout=5)
            except urllib.error.HTTPError as e:
                self.sent_on.append(e.headers.get("Location"))
        t = threading.Thread(target=go, daemon=True)
        self.going.append(t)
        t.start()
        return True


@pytest.fixture
def browser(monkeypatch):
    import webbrowser
    b = Browser()
    monkeypatch.setattr(webbrowser, "open", b.open)
    return b
