"""Who is performing the current action (written to the audit log).

There is no login yet, so the web app sends the name each person types into the top bar.
A context variable keeps concurrent requests from mixing up their names.
"""
import contextvars
from contextlib import contextmanager

_ACTOR = contextvars.ContextVar("cafebooks_actor", default="")


def current_actor():
    return _ACTOR.get()


def clean_actor(name, fallback=""):
    text = " ".join(str(name or "").split())[:60]
    return "".join(ch for ch in text if ch.isprintable()) or fallback


@contextmanager
def acting_as(name):
    token = _ACTOR.set(clean_actor(name))
    try:
        yield
    finally:
        _ACTOR.reset(token)
