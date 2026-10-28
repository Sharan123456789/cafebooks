import logging
import sys
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from database.excel_manager import ExcelManager, StorageError
from database.sync_manager import SyncManager
from services.accounting_service import AccountingService
from services.reporting_service import ReportingService
from utils.config import load_config, resolve_dir, app_dir
from utils.logging_config import setup_logging


def main():
    setup_logging(app_dir() / "logs" / "cafebooks.log")
    log = logging.getLogger("cafebooks")
    cfg = load_config()
    ctk.set_appearance_mode(cfg["appearance_mode"])
    ctk.set_default_color_theme(cfg["color_theme"])
    try:
        db = ExcelManager(resolve_dir(cfg, "data_dir"), resolve_dir(cfg, "backup_dir"), cfg["backup_retention"])
        db.acquire_instance_lock()
    except StorageError as exc:
        log.error("Startup failed: %s", exc)
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("CafeBooks cannot start safely", f"{exc}\n\nNo data was changed or overwritten.")
        return 1
    if db.recovery_message:
        log.warning(db.recovery_message)
    accounting = AccountingService(db, cfg)
    reports = ReportingService(accounting)
    sync = SyncManager(db, resolve_dir(cfg, "staging_dir"))
    from ui.main_window import CafeBooksApp
    app = CafeBooksApp(cfg, db, accounting, reports, sync)
    if db.recovery_message:
        app.after(500, lambda: messagebox.showinfo("Recovery", db.recovery_message, parent=app))
    app.after(100, lambda: __import__("threading").Thread(target=db.warm_cache, daemon=True).start())
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
