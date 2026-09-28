"""Entry point for the packaged macOS app.

Runs the FastAPI server on a background thread, opens the dashboard in the
default browser once it's healthy, and shows a menu-bar icon with a Quit
item — the app has no dock window, so this is the only visible chrome.
"""
from __future__ import annotations

import threading
import time
import webbrowser

import httpx
import rumps
import uvicorn

PORT = 8000
URL = f"http://localhost:{PORT}"


def _run_server() -> None:
    uvicorn.run("epic_compliance.api:app", host="127.0.0.1", port=PORT, log_level="warning")


def _wait_then_open_browser() -> None:
    for _ in range(60):
        try:
            if httpx.get(f"{URL}/healthz", timeout=1.0).status_code == 200:
                webbrowser.open(URL)
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)


class EpicComplianceApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Epic Compliance", icon=None, quit_button="Quit")
        self.menu = ["Open Dashboard"]

    @rumps.clicked("Open Dashboard")
    def open_dashboard(self, _sender: object) -> None:
        webbrowser.open(URL)


def main() -> None:
    threading.Thread(target=_run_server, daemon=True).start()
    threading.Thread(target=_wait_then_open_browser, daemon=True).start()
    EpicComplianceApp().run()


if __name__ == "__main__":
    main()
