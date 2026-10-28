import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file):
    """File logging with rotation.  Never log full records or credentials."""
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_cafebooks", False) for h in root.handlers):
        return root
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._cafebooks = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return root
