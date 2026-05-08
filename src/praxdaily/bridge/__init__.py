"""WeChat bridge — multi-persona × wxid routing on top of iLink.

Maps the chat APP's notion of "virtual character + user" to physical
WeChat accounts (iLink-controlled bots). Each persona can claim its own
wxid; if multiple personas share a wxid, outgoing messages are prefixed
with `【persona.name】` and inbound replies route via sticky last-active.

Storage: a single SQLite file at ``<cwd>/.prax/bridge.db``. Plain stdlib
sqlite3, no ORM — schema is small and the queries are mechanical.
"""

from .db import connect, db_path
from .schema import init_schema

from . import (
    alerts, bindings, binding_tokens, bots, broadcast, inbound, personas,
    rate_limit, replyctx, send,
)

__all__ = [
    "connect",
    "db_path",
    "init_schema",
    "alerts",
    "bindings",
    "binding_tokens",
    "bots",
    "broadcast",
    "inbound",
    "personas",
    "rate_limit",
    "replyctx",
    "send",
]
