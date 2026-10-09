"""Best-effort phone alerts. Set NTFY_TOPIC (free ntfy.sh app) and/or DISCORD_WEBHOOK_URL."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def notify(title: str, message: str, urgent: bool = False):
    log.info("NOTIFY %s: %s", title, message)
    try:
        import requests
    except ImportError:
        return
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=message.encode(), timeout=10,
                          headers={"Title": title, "Priority": "urgent" if urgent else "default"})
        except Exception as e:  # never let alerting break trading
            log.warning("ntfy failed: %s", e)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": f"**{title}**\n{message}"[:1900]}, timeout=10)
        except Exception as e:
            log.warning("discord failed: %s", e)
