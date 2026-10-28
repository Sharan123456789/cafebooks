"""ASGI entrypoint.  Vercel detects `app` here automatically; any ASGI server can also run it:

    uvicorn app:app            (or: python -m cafebooks serve)

Configuration comes from environment variables - see .env.example and README.md.
"""
import logging

from cafebooks.web.server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
