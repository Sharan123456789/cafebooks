"""Standalone desktop app.

Runs the same web app on this computer only (127.0.0.1) against a local SQLite database and
shows it in its own window (pywebview).  Without pywebview it opens the default browser and
keeps a small control window.  Data lives in %LOCALAPPDATA%\\CafeBooks for the packaged app,
or ./data when run from source (override with CAFEBOOKS_DATA_DIR).
"""
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

PREFERRED_PORT = 8765
log = logging.getLogger("cafebooks.desktop")


def _existing_instance(port):
    """True if a CafeBooks desktop server is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            info = json.loads(r.read().decode("utf-8"))
        return info.get("app") == "CafeBooks" and info.get("mode") == "desktop"
    except Exception:  # noqa: BLE001
        return False


def _free_port(preferred):
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free network port is available on this computer.")


def _show_error(title, message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(f"{title}: {message}", file=sys.stderr)


def _control_window(url):
    """Fallback when pywebview is not installed: a tiny window that keeps the app running."""
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001
        print(f"CafeBooks is running at {url} - press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    root = tk.Tk()
    root.title("CafeBooks")
    root.configure(bg="#171610")
    root.resizable(False, False)
    tk.Label(root, text="CafeBooks is running", fg="#f4b323", bg="#171610", font=("Segoe UI", 14, "bold")).pack(padx=24, pady=(18, 4))
    tk.Label(root, text=f"Open {url} in your browser.\nClose this window to stop CafeBooks.", fg="#d7d1c2", bg="#171610",
             justify="center").pack(padx=24, pady=(0, 12))
    row = tk.Frame(root, bg="#171610")
    row.pack(pady=(0, 18))
    tk.Button(row, text="Open CafeBooks", command=lambda: webbrowser.open(url), width=16).pack(side="left", padx=6)
    tk.Button(row, text="Quit", command=root.destroy, width=10).pack(side="left", padx=6)
    root.mainloop()


def main():
    os.environ.setdefault("CAFEBOOKS_MODE", "desktop")
    from cafebooks.utils.config import ServerSettings
    from cafebooks.utils.logging_config import setup_logging
    settings = ServerSettings.from_env()
    setup_logging(settings.data_dir / "logs" / "cafebooks.log")

    if _existing_instance(PREFERRED_PORT):
        webbrowser.open(f"http://127.0.0.1:{PREFERRED_PORT}/")      # already open: just show it
        return 0

    from cafebooks.web.server import create_app, get_runtime, reset_runtime
    try:
        rt = get_runtime()
    except Exception as exc:  # noqa: BLE001
        log.exception("Startup failed")
        _show_error("CafeBooks cannot start safely", f"{exc}\n\nNo data was changed.")
        return 1
    if rt.config.get("auto_backup_daily", True):
        try:
            rt.db.maybe_daily_backup()
        except Exception:  # noqa: BLE001
            log.exception("Daily backup failed")

    import uvicorn
    port = _free_port(PREFERRED_PORT)
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, name="cafebooks-server", daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/"
    log.info("CafeBooks desktop running at %s (data: %s)", url, settings.data_dir)
    try:
        try:
            import webview
        except ImportError:
            webview = None
        if webview is not None:
            try:
                webview.settings["ALLOW_DOWNLOADS"] = True
            except Exception:  # noqa: BLE001  (older pywebview)
                pass
            webview.create_window("CafeBooks", url, width=1360, height=880, min_size=(1000, 680),
                                  background_color="#171610")
            webview.start()
        else:
            webbrowser.open(url)
            _control_window(url)
    finally:
        try:
            if rt.config.get("auto_backup_daily", True):
                rt.db.maybe_daily_backup()
        except Exception:  # noqa: BLE001
            log.exception("Daily backup failed")
        server.should_exit = True
        thread.join(timeout=5)
        reset_runtime()
    return 0


if __name__ == "__main__":
    sys.exit(main())
