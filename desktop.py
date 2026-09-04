import os
import sys
import threading
import time
import webview
from dotenv import load_dotenv

# Load .env from EXE folder or current folder
if getattr(sys, "frozen", False):
    env_path = os.path.join(os.path.dirname(sys.executable), ".env")
else:
    env_path = ".env"

    load_dotenv(env_path)

from app import app

# pywebview blocks all file downloads by default (a security default).
# This must be set BEFORE webview.create_window()/webview.start(),
# otherwise send_file(as_attachment=True) responses (your PDF/Excel
# exports) get silently swallowed instead of triggering a save dialog.
webview.settings['ALLOW_DOWNLOADS'] = True


def run_server():
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False,
        threaded=True
    )


if __name__ == "__main__":

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )
    server_thread.start()

    # Wait for Flask to start
    time.sleep(2)

    webview.create_window(
        title="Finance Collection Management System",
        url="http://127.0.0.1:5000",
        width=1400,
        height=850,
        min_size=(1100, 700),
        resizable=True
    )

    webview.start()