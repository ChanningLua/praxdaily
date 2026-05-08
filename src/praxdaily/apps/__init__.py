"""Application layer — features that consume the bridge platform.

Each app subscribes to bridge inbound events and uses bridge.send /
bridge.broadcast for outbound. Apps don't share state with each other;
they're discrete features that all happen to ride on the same WeChat
transport layer.

Currently shipped:

  - ``daily_qa`` — handles user replies to the daily digest.
    Commands: 今天 / 昨天 / 查 <kw> / 退订 / 帮助.
"""

from . import daily_qa

__all__ = ["daily_qa"]
