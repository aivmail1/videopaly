# -*- coding: utf-8 -*-
"""监控轮询纯线程：定时拉取多个 JSON 源，把条目回调给 UI 层匹配/去重。"""
from __future__ import annotations

import threading
import time

import parser


class MonitorPoller(threading.Thread):
    daemon = True

    def __init__(self, on_items=None, on_log=None):
        super().__init__()
        self.on_items = on_items
        self.on_log = on_log
        # 不能叫 self._stop：会覆盖 threading.Thread._stop() 方法，
        # 导致 join() 时抛 "TypeError: 'Event' object is not callable"
        self._stop_evt = threading.Event()
        self.sources: list[tuple[str, bool]] = []
        self.interval = 30

    def set_params(self, sources: list[tuple[str, bool]], interval: int):
        self.sources = sources
        self.interval = max(5, interval)

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.is_set():
            for url, enabled in self.sources:
                if self._stop_evt.is_set():
                    break
                if not enabled:
                    continue
                try:
                    items = parser.load_json_url(url, timeout=15,
                                                 proxies=parser.merged_proxies())
                    if self.on_items:
                        self.on_items(items, url)
                    if self.on_log:
                        self.on_log(f"[{url}] 本轮拉取 {len(items)} 条")
                except Exception as e:  # noqa: BLE001
                    if self.on_log:
                        self.on_log(f"[{url}] 拉取失败：{e}")
            slept = 0
            while slept < self.interval and not self._stop_evt.is_set():
                time.sleep(1)
                slept += 1
