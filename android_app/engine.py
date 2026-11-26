# -*- coding: utf-8 -*-
"""纯线程下载/录制引擎（无 Qt、无 Kivy）。

所有任务在后台 daemon 线程跑，通过回调上报；回调可能在非主线程触发，
UI 层（Kivy）须用 Clock.schedule_once 切回主线程更新。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import requests

from parser import (
    fetch_text, resolve_variant, parse_media, aes_decrypt,
    safe_filename, kind_of, merged_headers, merged_proxies,
    apply_filename_template,
)


class _RateLimiter:
    """简单令牌桶式限速：limit 为 bytes/s，0 表示不限速。"""

    def __init__(self, limit: float = 0.0):
        self.limit = limit
        self._bytes = 0
        self._t0 = time.time()

    def add(self, n: int):
        if self.limit <= 0 or n <= 0:
            return
        self._bytes += n
        elapsed = time.time() - self._t0
        expected = self._bytes / self.limit
        if expected > elapsed:
            time.sleep(min(expected - elapsed, 1.0))


class BaseTask(threading.Thread):
    daemon = True

    def __init__(self, url: str, save_dir: str, title: str,
                 on_progress=None, on_status=None, on_done=None, on_fail=None,
                 headers: dict | None = None, proxy: str | None = None,
                 speed_limit: float = 0.0, name_template: str = "",
                 quality: str = "", file_base: str = ""):
        super().__init__()
        self.url = url
        self.save_dir = save_dir
        self.title = title
        self.on_progress = on_progress
        self.on_status = on_status
        self.on_done = on_done
        self.on_fail = on_fail
        self.headers = headers
        self.proxy = proxy
        self.speed_limit = speed_limit
        self.name_template = name_template
        self.quality = quality
        # 预计算文件名（重试时由外层传入同一值，避免文件名漂移导致 .part 断点续传失效）
        self._file_base_value = file_base
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def _status(self, s: str):
        if self.on_status:
            self.on_status(s)

    def _progress(self, done, total, speed=0.0):
        if self.on_progress:
            self.on_progress(done, total, speed)

    def _headers(self, extra: dict | None = None) -> dict:
        """请求头：先取解析层合并后的自定义头，self.headers 与 extra 追加覆盖。"""
        h = merged_headers(extra)
        if self.headers:
            h.update(self.headers)
        return h

    def _proxies(self) -> dict | None:
        return merged_proxies(self.proxy)

    def _file_base(self) -> str:
        """按命名模板生成安全文件名（不含扩展名）；优先用预计算值，只算一次并缓存。"""
        if self._file_base_value:
            return self._file_base_value
        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._file_base_value = apply_filename_template(
            self.name_template, self.title, time_str,
            quality=self.quality, title=self.title)
        return self._file_base_value


class Mp4Downloader(BaseTask):
    """MP4 / 普通文件直链流式下载，断点续传，落盘后校验大小。"""

    def _target_ext(self) -> str:
        low = self.url.lower().split("?")[0]
        for ext in (".mp4", ".webm", ".mov", ".mkv", ".avi"):
            if low.endswith(ext):
                return ext
        return ".mp4"

    def run(self):
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            base = self._file_base()
            final = os.path.join(self.save_dir, base + self._target_ext())
            part = final + ".part"
            headers = self._headers()
            proxies = self._proxies()
            resume = 0
            if os.path.exists(part):
                resume = os.path.getsize(part)
                headers["Range"] = f"bytes={resume}-"

            self._status("连接中…")
            resp = requests.get(self.url, headers=headers, proxies=proxies,
                                stream=True, timeout=20)
            total = int(resp.headers.get("Content-Length", 0))
            if resp.status_code == 206:
                total += resume
            elif resp.status_code != 200:
                resp.close()
                if self.on_fail:
                    self.on_fail(f"HTTP {resp.status_code}")
                return

            mode = "ab" if resume > 0 and resp.status_code == 206 else "wb"
            done = resume
            last_t, last_bytes = time.time(), resume
            limiter = _RateLimiter(self.speed_limit)

            with open(part, mode) as f:
                for chunk in resp.iter_content(1024 * 256):
                    if self.stopped:
                        resp.close()
                        self._status("已取消")
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    limiter.add(len(chunk))
                    done += len(chunk)
                    now = time.time()
                    if now - last_t >= 0.3:
                        speed = (done - last_bytes) / (now - last_t)
                        self._progress(done, total, speed)
                        last_t, last_bytes = now, done
            resp.close()

            # 完整性校验：Content-Length 已知时比对落盘大小，不一致交重试重下
            if total > 0 and done < total:
                if self.on_fail:
                    self.on_fail(f"下载不完整：{done}/{total} 字节")
                return
            if os.path.exists(final):
                os.remove(final)
            os.replace(part, final)
            self._progress(done, total, 0.0)
            if self.on_done:
                self.on_done(final)
        except requests.RequestException as e:
            if self.on_fail:
                self.on_fail(f"网络错误：{e}")
        except Exception as e:  # noqa: BLE001
            if self.on_fail:
                self.on_fail(f"下载失败：{e}")


class HlsDownloader(BaseTask):
    """点播 m3u8：解析、分片下载、AES-128 解密、合并为 ts（后续可转 mp4）。"""

    def __init__(self, url: str, save_dir: str, title: str,
                 variant_url: str | None = None, **kw):
        super().__init__(url, save_dir, title, **kw)
        # 非空：直接抓该档清晰度，跳过 master 选档；空：走原 master 选最高档逻辑。
        self.variant_url = variant_url

    def run(self):
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            base = self._file_base()
            out_path = os.path.join(self.save_dir, base + ".mp4")
            part = out_path + ".ts.part"
            proxies = self._proxies()

            self._status("解析 m3u8…")
            if self.variant_url:
                # 已指定清晰度：直接抓该档，避免先抓 master 再抓 variant 的双请求
                text = fetch_text(self.variant_url, self._headers(), proxies)
            else:
                text = fetch_text(self.url, self._headers(), proxies)
                if "#EXT-X-STREAM-INF" in text:
                    variant = resolve_variant(text, self.url)
                    if not variant:
                        if self.on_fail:
                            self.on_fail("master 列表里没有可用清晰度")
                        return
                    text = fetch_text(variant, self._headers(), proxies)

            segments, key_info, _ended = parse_media(text, self.url)
            if not segments:
                if self.on_fail:
                    self.on_fail("m3u8 中没有可下载的分片")
                    return

            key = None
            if key_info:
                from parser import _CRYPTO_OK
                if not _CRYPTO_OK:
                    if self.on_fail:
                        self.on_fail("分片已加密，缺少 cryptography")
                    return
                key = requests.get(key_info["uri"], headers=self._headers(),
                                   proxies=proxies, timeout=15).content

            total, done, bytes_done = len(segments), 0, 0
            last_t, last_bytes = time.time(), 0
            limiter = _RateLimiter(self.speed_limit)
            with open(part, "wb") as out:
                for idx, seg in enumerate(segments):
                    if self.stopped:
                        self._status("已取消")
                        return
                    r = requests.get(seg, headers=self._headers({"Referer": self.url}),
                                     proxies=proxies, timeout=20)
                    r.raise_for_status()
                    data = r.content
                    if key:
                        data = aes_decrypt(data, key, key_info.get("iv"), idx)
                    out.write(data)
                    out.flush()
                    limiter.add(len(data))
                    bytes_done += len(data)
                    done += 1
                    now = time.time()
                    if now - last_t >= 0.3:
                        speed = (bytes_done - last_bytes) / (now - last_t)
                        self._progress(done, total, speed)
                        last_t, last_bytes = now, bytes_done

            # 完整性校验：分片数不足交重试重下
            if done < total:
                if self.on_fail:
                    self.on_fail(f"分片不完整：{done}/{total}")
                return
            if os.path.exists(out_path):
                os.remove(out_path)
            os.replace(part, out_path)
            self._progress(total, total, 0.0)
            if self.on_done:
                self.on_done(out_path)
        except requests.RequestException as e:
            if self.on_fail:
                self.on_fail(f"网络错误：{e}")
        except Exception as e:  # noqa: BLE001
            if self.on_fail:
                self.on_fail(f"m3u8 下载失败：{e}")


class LiveRecorder(BaseTask):
    """直播录制：循环刷新 m3u8 抓新分片追加写 ts；max_seconds=0 持续。

    segment_minutes>0 时按分钟切段：旧文件关闭后开新文件 `{base}_{i:03d}.ts`，
    路径收集到 self.segments；on_done 回调收到首段路径。
    断流（网络异常）按 5/10/20s 指数退避重连，已录分片保留、续接写同一目录；
    重连次数耗尽才 on_fail。
    """

    def __init__(self, url, save_dir, title, max_seconds: int = 0,
                 segment_minutes: int = 0, max_reconnect: int = 5, **kw):
        super().__init__(url, save_dir, title, **kw)
        self.max_seconds = max_seconds
        self.segment_minutes = segment_minutes
        self.max_reconnect = max_reconnect
        self.segments: list[str] = []

    def _seg_path(self, base: str, idx: int) -> str:
        if self.segment_minutes > 0:
            return os.path.join(self.save_dir, f"{base}_{idx:03d}.ts")
        return os.path.join(self.save_dir, base + ".ts")

    def _reconnect_sleep(self, attempt: int):
        """退避 5/10/20s（上限 20s），分段睡眠便于快速取消。"""
        delay = min(20.0, 5.0 * (2 ** (attempt - 1)))
        slept = 0.0
        while slept < delay and not self.stopped:
            time.sleep(1)
            slept += 1

    def run(self):
        out = None
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            base = self._file_base() + "_录制"
            seg_index = 0
            out_path = self._seg_path(base, seg_index)
            self.segments.append(out_path)
            seen: set[str] = set()
            start = time.time()
            seg_start = time.time()
            key, key_info = None, None
            proxies = self._proxies()
            reconnects = 0

            out = open(out_path, "wb")
            while not self.stopped and (self.max_seconds <= 0 or time.time() - start < self.max_seconds):
                try:
                    # 分段录制：到达分段时长后关闭旧文件、开新文件
                    if self.segment_minutes > 0 and \
                            time.time() - seg_start >= self.segment_minutes * 60:
                        out.close()
                        seg_index += 1
                        out_path = self._seg_path(base, seg_index)
                        self.segments.append(out_path)
                        out = open(out_path, "wb")
                        seg_start = time.time()
                        self._status(f"分段 {seg_index + 1}")

                    text = fetch_text(self.url, self._headers(), proxies)
                    if "#EXT-X-STREAM-INF" in text:
                        variant = resolve_variant(text, self.url)
                        if variant:
                            text = fetch_text(variant, self._headers(), proxies)
                    segments, ki, ended = parse_media(text, self.url)
                    if ki and key is None:
                        from parser import _CRYPTO_OK
                        if not _CRYPTO_OK:
                            out.close()
                            if self.on_fail:
                                self.on_fail("分片已加密，缺少 cryptography")
                            return
                        key = requests.get(ki["uri"], headers=self._headers(),
                                           proxies=proxies, timeout=15).content
                        key_info = ki
                    for seg in segments:
                        if seg in seen:
                            continue
                        seen.add(seg)
                        r = requests.get(seg, headers=self._headers({"Referer": self.url}),
                                         proxies=proxies, timeout=20)
                        r.raise_for_status()
                        data = r.content
                        if key:
                            data = aes_decrypt(data, key, key_info.get("iv"), len(seen))
                        out.write(data)
                        out.flush()
                        if self.on_progress:
                            self.on_progress(len(seen), time.time() - start, 0.0)
                    reconnects = 0  # 本轮成功，重置重连计数
                    if ended:
                        break
                    # 3 秒轮询，分段睡眠便于快速取消
                    slept = 0
                    while slept < 3 and not self.stopped:
                        time.sleep(1)
                        slept += 1
                except requests.RequestException as e:
                    if self.stopped:
                        break
                    reconnects += 1
                    if reconnects > self.max_reconnect:
                        out.close()
                        if self.on_fail:
                            self.on_fail(f"断流重连 {self.max_reconnect} 次仍失败：{e}")
                        return
                    self._status(f"断流，{reconnects} 次重连中…")
                    self._reconnect_sleep(reconnects)
            out.close()
            if self.on_done:
                self.on_done(self.segments[0])
        except requests.RequestException as e:
            if out:
                out.close()
            if self.on_fail:
                self.on_fail(f"录制网络错误：{e}")
        except Exception as e:  # noqa: BLE001
            if out:
                out.close()
            if self.on_fail:
                self.on_fail(f"录制失败：{e}")


class RetryRunner(BaseTask):
    """失败自动重试包装：按指数退避重建 inner 任务重跑，耗尽才回调 on_fail。

    inner_factory: 每次重试返回一个新的 BaseTask 实例；
    max_retries: 首次失败后的最大重试次数；backoff_seconds: 退避基数秒。
    用户 stop() 后不再重试，直接退出。
    """

    def __init__(self, inner_factory, max_retries: int = 3,
                 backoff_seconds: float = 2.0, **cb):
        super().__init__("", "", "", **cb)
        self._factory = inner_factory
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._inner: BaseTask | None = None

    def stop(self):
        super().stop()
        if self._inner:
            self._inner.stop()

    def _backoff_sleep(self, attempt: int):
        """指数退避：min(30, base * 2**attempt)，分段睡眠便于快速取消。"""
        delay = min(30.0, self.backoff_seconds * (2 ** attempt))
        slept = 0.0
        while slept < delay and not self.stopped:
            time.sleep(1)
            slept += 1

    def run(self):
        attempt = 0
        fail_msg = ""
        while True:
            if self.stopped:
                return
            inner = self._factory()
            self._inner = inner
            succeeded = {"ok": False}

            def _on_done(path, _inner=inner):
                succeeded["ok"] = True
                if self.on_done:
                    self.on_done(path)

            def _on_fail(msg, _inner=inner):
                if not succeeded["ok"]:
                    fail_msg = msg

            inner.on_progress = self.on_progress
            inner.on_status = self.on_status
            inner.on_done = _on_done
            inner.on_fail = _on_fail
            inner.start()
            inner.join()

            if self.stopped:
                return
            if succeeded["ok"]:
                return

            attempt += 1
            if attempt > self.max_retries:
                if self.on_fail:
                    self.on_fail(fail_msg or "多次重试后仍失败")
                return
            self._status(f"失败，第 {attempt} 次重试…")
            self._backoff_sleep(attempt - 1)


def build_task(url: str, save_dir: str, title: str, mode: str,
               max_seconds: int = 0, kind_override: str | None = None,
               max_retries: int = 3, headers: dict | None = None,
               variant_url: str | None = None, segment_minutes: int = 0,
               proxy: str | None = None, speed_limit: float = 0.0,
               name_template: str = "", quality: str = "",
               max_reconnect: int = 5, **cb) -> BaseTask:
    """mode: download / record；kind_override 强制 mp4/hls，None 自动识别。

    max_retries: 失败重试次数（0=不重试）；headers: 自定义请求头；
    variant_url: 指定清晰度直链；segment_minutes: 直播分段分钟数（0=不切）；
    proxy: 代理地址；speed_limit: 限速 bytes/s（0=不限）；name_template: 命名模板；
    quality: 清晰度标注；max_reconnect: 直播断流重连次数。
    """
    # 文件名在重试外层算一次：重试重建任务复用同一名字，
    # MP4 的 .part 断点续传与覆盖旧半成品文件都依赖名字稳定
    fb = _preset_file_base(url, title, name_template, quality)

    def factory() -> BaseTask:
        if mode == "record":
            return LiveRecorder(url, save_dir, title, max_seconds=max_seconds,
                                segment_minutes=segment_minutes, proxy=proxy,
                                speed_limit=speed_limit, name_template=name_template,
                                quality=quality, max_reconnect=max_reconnect,
                                file_base=fb, **cb)
        kind = kind_override or kind_of(url)
        if kind == "hls":
            return HlsDownloader(url, save_dir, title, variant_url=variant_url,
                                 headers=headers, proxy=proxy, speed_limit=speed_limit,
                                 name_template=name_template, quality=quality,
                                 file_base=fb, **cb)
        return Mp4Downloader(url, save_dir, title, headers=headers, proxy=proxy,
                             speed_limit=speed_limit, name_template=name_template,
                             quality=quality, file_base=fb, **cb)

    if max_retries and max_retries > 0:
        return RetryRunner(factory, max_retries=max_retries, **cb)
    return factory()


def _preset_file_base(url: str, title: str, name_template: str,
                       quality: str) -> str:
    """在 build_task 外层把文件名算好一次，重试时所有 inner 任务复用同一名字。"""
    time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return apply_filename_template(name_template, title, time_str,
                                   quality=quality, title=title)
