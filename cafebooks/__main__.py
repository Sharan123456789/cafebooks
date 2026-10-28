"""Command line:

    python -m cafebooks serve [--host 127.0.0.1] [--port 8000]   run the web app (LAN / VPS / development)
    python -m cafebooks desktop                                   run the standalone desktop app
    python -m cafebooks import-excel <folder> [--force]           import an earlier CafeBooks Excel data folder
    python -m cafebooks backup [--label manual]                   create a backup now
    python -m cafebooks check                                     run the integrity check
"""
import argparse
import logging
import os
import sys
from pathlib import Path


def load_dotenv(path=None):
    """Read KEY=VALUE lines from .env (existing environment variables win)."""
    from cafebooks.utils.config import project_dir
    path = Path(path) if path else project_dir() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _services():
    from cafebooks.web.server import get_runtime
    return get_runtime()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cafebooks", description="CafeBooks cafe accounting")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices on your network")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("desktop", help="run the desktop app")
    imp = sub.add_parser("import-excel", help="import an earlier CafeBooks Excel data folder")
    imp.add_argument("folder")
    imp.add_argument("--force", action="store_true", help="import even if validation finds problems")
    bk = sub.add_parser("backup", help="create a backup")
    bk.add_argument("--label", default="manual")
    sub.add_parser("check", help="run the integrity check")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "serve":
        import uvicorn
        from cafebooks.web.server import create_app
        _services()                                  # fail fast with a clear message
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    if args.command == "desktop":
        from cafebooks.desktop import main as desktop_main
        return desktop_main()
    rt = _services()
    if args.command == "import-excel":
        res = rt.data.import_legacy_folder(args.folder, force=args.force)
        print(f"Imported. Safety backup: {res['backup']}")
        for table, n in res["tables"].items():
            print(f"  {table}: {n} rows")
        for p in res["problems"][:20]:
            print(f"  problem: {p}")
        return 0
    if args.command == "backup":
        print(rt.db.backup(args.label))
        return 0
    if args.command == "check":
        issues = rt.accounting.integrity_check()
        print("\n".join(issues) if issues else "All checks passed.")
        return 1 if any(i.startswith("ERROR") for i in issues) else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
