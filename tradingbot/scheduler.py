"""24/7 loop that drives the desk on the US market calendar (via the broker clock)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime

from .live import NY, Desk
from .notify import notify

log = logging.getLogger(__name__)

MORNING_AT = dtime(9, 31)
ENTRY_WINDOW_END = dtime(10, 0)   # later than this, entries are skipped for the day
EOD_AT = dtime(16, 10)
MONITOR_EVERY_S = 300
TICK_S = 30


class Scheduler(threading.Thread):
    def __init__(self, desk: Desk):
        super().__init__(daemon=True, name="scheduler")
        self.desk = desk
        self.last_monitor = 0.0
        self.errors = 0
        self.stop_event = threading.Event()

    def tick(self):
        st = self.desk.store
        now = datetime.now(NY)
        today = now.date().isoformat()
        clock = self.desk.broker.clock()
        st.set("heartbeat", now.isoformat(timespec="seconds"))
        if clock.get("is_open"):
            if st.get("morning_done") != today and now.time() >= MORNING_AT:
                allow = now.time() <= ENTRY_WINDOW_END
                if not allow:
                    st.log("warn", "started after the opening window: exits only today")
                self.desk.morning(allow_entries=allow)
                self.last_monitor = time.time()
            elif time.time() - self.last_monitor >= MONITOR_EVERY_S:
                self.desk.monitor()
                self.last_monitor = time.time()
        elif st.get("morning_done") == today and st.get("eod_done") != today and now.time() >= EOD_AT:
            self.desk.end_of_day()

    def run(self):
        log.info("scheduler started")
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.errors = 0
            except Exception as e:
                self.errors += 1
                log.exception("scheduler tick failed")
                try:
                    self.desk.store.log("error", f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                if self.errors in (3, 20):     # alert once early, once if it keeps failing
                    notify("Trading desk error", f"{self.errors} failures in a row: {e}", urgent=True)
            self.stop_event.wait(TICK_S)
