"""
VideoPlayerDL Download & Live Recording Engine (Optimized)
Supports MP4 (with Range resume), HLS/M3U8 (with AES-128 decryption & concurrency),
Live Stream recording, and Exponential Backoff Retry.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "*/*",
}


def create_session(
    headers: Optional[Dict[str, str]] = None,
    pool_connections: int = 16,
    pool_maxsize: int = 16,
) -> requests.Session:
    """Create a pooled HTTP session with sensible headers."""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    session.headers.update(merged_headers)
    return session


def _aes128_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AES-128-CBC encrypted HLS segment with PKCS#7 unpadding."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography library is required for encrypted HLS streams")
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    if not padded:
        return b""
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


@dataclass
class M3U8Segment:
    uri: str
    duration: float = 0.0
    key_uri: Optional[str] = None
    iv: Optional[bytes] = None
    sequence: int = 0


@dataclass
class M3U8Playlist:
    is_master: bool = False
    is_endlist: bool = True
    target_duration: float = 5.0
    media_sequence: int = 0
    variants: List[Tuple[int, str]] = field(default_factory=list)
    segments: List[M3U8Segment] = field(default_factory=list)


def parse_m3u8(content: str, base_url: str) -> M3U8Playlist:
    """Parse master or media M3U8 playlist."""
    playlist = M3U8Playlist()
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    current_key_uri: Optional[str] = None
    current_iv: Optional[bytes] = None
    pending_duration: float = 0.0
    pending_bandwidth: Optional[int] = None
    seq = 0

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                playlist.media_sequence = int(line.split(":", 1)[1].strip())
                seq = playlist.media_sequence
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                playlist.target_duration = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.is_endlist = True
        elif line.startswith("#EXT-X-STREAM-INF:"):
            playlist.is_master = True
            bw_match = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bandwidth = int(bw_match.group(1)) if bw_match else 0
        elif line.startswith("#EXT-X-KEY:"):
            method_match = re.search(r"METHOD=([^,]+)", line)
            method = method_match.group(1).strip() if method_match else "NONE"
            if method == "NONE":
                current_key_uri = None
                current_iv = None
            elif method == "AES-128":
                uri_match = re.search(r'URI="([^"]+)"', line)
                if uri_match:
                    current_key_uri = urljoin(base_url, uri_match.group(1))
                iv_match = re.search(r"IV=0x([0-9a-fA-F]+)", line)
                if iv_match:
                    iv_hex = iv_match.group(1).zfill(32)
                    current_iv = bytes.fromhex(iv_hex[:32])
                else:
                    current_iv = None
        elif line.startswith("#EXTINF:"):
            dur_str = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_duration = float(dur_str)
            except ValueError:
                pending_duration = 0.0
        elif not line.startswith("#"):
            resolved = urljoin(base_url, line)
            if playlist.is_master and pending_bandwidth is not None:
                playlist.variants.append((pending_bandwidth, resolved))
                pending_bandwidth = None
            else:
                seg_iv = (
                    current_iv
                    if current_iv is not None
                    else seq.to_bytes(16, byteorder="big")
                )
                playlist.segments.append(
                    M3U8Segment(
                        uri=resolved,
                        duration=pending_duration,
                        key_uri=current_key_uri,
                        iv=seg_iv if current_key_uri else None,
                        sequence=seq,
                    )
                )
                pending_duration = 0.0
                seq += 1

    if not playlist.is_master and "#EXT-X-ENDLIST" not in content:
        playlist.is_endlist = False

    playlist.variants.sort(key=lambda item: item[0], reverse=True)
    return playlist


class BaseTask(threading.Thread):
    """Base worker thread supporting cancellation and unified progress callbacks."""

    def __init__(
        self,
        url: str,
        output_path: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Tuple[float, float] = (10.0, 30.0),
        on_progress: Optional[Callable[[int, int, float], None]] = None,
        on_done: Optional[Callable[[str], None]] = None,
        on_fail: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.output_path = output_path
        self.headers = headers or {}
        self.timeout = timeout
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_fail = on_fail
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _emit_progress(self, downloaded: int, total: int, speed: float) -> None:
        if self.on_progress:
            self.on_progress(downloaded, total, speed)

    def _emit_done(self, path: str) -> None:
        if self.on_done:
            self.on_done(path)

    def _emit_fail(self, reason: str) -> None:
        if self.on_fail:
            self.on_fail(reason[:500])


class Mp4Downloader(BaseTask):
    """Direct file downloader supporting HTTP Range resume."""

    def __init__(
        self,
        url: str,
        output_path: str,
        chunk_size: int = 256 * 1024,
        **kwargs,
    ) -> None:
        super().__init__(url, output_path, **kwargs)
        self.chunk_size = chunk_size

    def run(self) -> None:
        part_path = f"{self.output_path}.part"
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
        session = create_session(self.headers)

        try:
            existing_bytes = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            req_headers = {}
            if existing_bytes > 0:
                req_headers["Range"] = f"bytes={existing_bytes}-"

            with session.get(
                self.url, headers=req_headers, stream=True, timeout=self.timeout
            ) as resp:
                if resp.status_code == 416 and existing_bytes > 0:
                    os.replace(part_path, self.output_path)
                    self._emit_progress(existing_bytes, existing_bytes, 0.0)
                    self._emit_done(self.output_path)
                    return

                resp.raise_for_status()

                if resp.status_code == 206:
                    mode = "ab"
                    downloaded = existing_bytes
                    content_len = int(resp.headers.get("Content-Length", "0") or 0)
                    total = existing_bytes + content_len if content_len > 0 else 0
                else:
                    mode = "wb"
                    downloaded = 0
                    total = int(resp.headers.get("Content-Length", "0") or 0)

                last_tick = time.monotonic()
                bytes_since_tick = 0
                speed = 0.0

                with open(part_path, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=self.chunk_size):
                        if self.is_cancelled:
                            self._emit_fail("Cancelled by user")
                            return
                        if not chunk:
                            continue
                        fh.write(chunk)
                        n = len(chunk)
                        downloaded += n
                        bytes_since_tick += n

                        now = time.monotonic()
                        elapsed = now - last_tick
                        if elapsed >= 0.5:
                            speed = bytes_since_tick / elapsed
                            last_tick = now
                            bytes_since_tick = 0
                            self._emit_progress(downloaded, total, speed)

            os.replace(part_path, self.output_path)
            self._emit_progress(downloaded, downloaded, speed)
            self._emit_done(self.output_path)
        except Exception as exc:
            self._emit_fail(f"MP4 download error: {exc}")
        finally:
            session.close()


class HlsDownloader(BaseTask):
    """VOD M3U8/HLS downloader with parallel segment fetching and AES-128 decryption."""

    def __init__(
        self,
        url: str,
        output_path: str,
        max_workers: int = 4,
        **kwargs,
    ) -> None:
        super().__init__(url, output_path, **kwargs)
        self.max_workers = max(1, max_workers)
        self._key_cache: Dict[str, bytes] = {}
        self._key_lock = threading.Lock()

    def _get_key(self, session: requests.Session, key_uri: str) -> bytes:
        with self._key_lock:
            if key_uri in self._key_cache:
                return self._key_cache[key_uri]
        resp = session.get(key_uri, timeout=self.timeout)
        resp.raise_for_status()
        key = resp.content
        with self._key_lock:
            self._key_cache[key_uri] = key
        return key

    def _resolve_media_playlist(
        self, session: requests.Session, url: str
    ) -> Tuple[str, M3U8Playlist]:
        resp = session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        playlist = parse_m3u8(resp.text, resp.url)
        if playlist.is_master:
            if not playlist.variants:
                raise RuntimeError("Master M3U8 playlist contains no variants")
            best_url = playlist.variants[0][1]
            return self._resolve_media_playlist(session, best_url)
        return resp.url, playlist

    def _download_segment(
        self, session: requests.Session, idx: int, seg: M3U8Segment, seg_dir: str
    ) -> Tuple[int, int]:
        if self.is_cancelled:
            raise RuntimeError("Cancelled by user")
        seg_file = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
        if os.path.exists(seg_file) and os.path.getsize(seg_file) > 0:
            return idx, os.path.getsize(seg_file)

        resp = session.get(seg.uri, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.content
        if seg.key_uri and seg.iv is not None:
            key = self._get_key(session, seg.key_uri)
            data = _aes128_cbc_decrypt(data, key, seg.iv)

        tmp_file = f"{seg_file}.tmp"
        with open(tmp_file, "wb") as fh:
            fh.write(data)
        os.replace(tmp_file, seg_file)
        return idx, len(data)

    def run(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
        seg_dir = f"{self.output_path}.segments"
        os.makedirs(seg_dir, exist_ok=True)
        session = create_session(
            self.headers,
            pool_connections=self.max_workers * 2,
            pool_maxsize=self.max_workers * 2,
        )

        try:
            _, playlist = self._resolve_media_playlist(session, self.url)
            total_segs = len(playlist.segments)
            if total_segs == 0:
                raise RuntimeError("No media segments found in M3U8 playlist")

            completed_segs = 0
            downloaded_bytes = 0
            start_time = time.monotonic()

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers
            ) as executor:
                futures = [
                    executor.submit(self._download_segment, session, idx, seg, seg_dir)
                    for idx, seg in enumerate(playlist.segments)
                ]
                for future in concurrent.futures.as_completed(futures):
                    if self.is_cancelled:
                        for f in futures:
                            f.cancel()
                        self._emit_fail("Cancelled by user")
                        return
                    _, seg_size = future.result()
                    completed_segs += 1
                    downloaded_bytes += seg_size
                    elapsed = max(time.monotonic() - start_time, 1e-3)
                    self._emit_progress(
                        completed_segs, total_segs, downloaded_bytes / elapsed
                    )

            part_path = f"{self.output_path}.part"
            with open(part_path, "wb") as out_fh:
                for idx in range(total_segs):
                    seg_file = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                    with open(seg_file, "rb") as in_fh:
                        out_fh.write(in_fh.read())

            os.replace(part_path, self.output_path)
            for idx in range(total_segs):
                seg_file = os.path.join(seg_dir, f"seg_{idx:06d}.ts")
                if os.path.exists(seg_file):
                    os.remove(seg_file)
            try:
                os.rmdir(seg_dir)
            except OSError:
                pass

            self._emit_done(self.output_path)
        except Exception as exc:
            self._emit_fail(f"HLS download error: {exc}")
        finally:
            session.close()


class LiveRecorder(BaseTask):
    """Live HLS/M3U8 stream recorder with session reuse and sequence deduplication."""

    def __init__(
        self,
        url: str,
        output_path: str,
        max_duration: Optional[float] = None,
        **kwargs,
    ) -> None:
        super().__init__(url, output_path, **kwargs)
        self.max_duration = max_duration
        self._key_cache: Dict[str, bytes] = {}

    def _get_key(self, session: requests.Session, key_uri: str) -> bytes:
        if key_uri not in self._key_cache:
            resp = session.get(key_uri, timeout=self.timeout)
            resp.raise_for_status()
            self._key_cache[key_uri] = resp.content
        return self._key_cache[key_uri]

    def run(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
        part_path = f"{self.output_path}.part"
        session = create_session(self.headers)
        seen_sequences: set[int] = set()
        recorded_bytes = 0
        start_time = time.monotonic()

        try:
            # Resolve master playlist once if needed
            resp = session.get(self.url, timeout=self.timeout)
            resp.raise_for_status()
            first_pl = parse_m3u8(resp.text, resp.url)
            stream_url = (
                first_pl.variants[0][1]
                if first_pl.is_master and first_pl.variants
                else resp.url
            )

            with open(part_path, "ab") as out_fh:
                while not self.is_cancelled:
                    if (
                        self.max_duration is not None
                        and (time.monotonic() - start_time) >= self.max_duration
                    ):
                        break

                    pl_resp = session.get(stream_url, timeout=self.timeout)
                    pl_resp.raise_for_status()
                    playlist = parse_m3u8(pl_resp.text, pl_resp.url)

                    new_segments = [
                        s for s in playlist.segments if s.sequence not in seen_sequences
                    ]
                    for seg in new_segments:
                        if self.is_cancelled:
                            break
                        seg_resp = session.get(seg.uri, timeout=self.timeout)
                        seg_resp.raise_for_status()
                        data = seg_resp.content
                        if seg.key_uri and seg.iv is not None:
                            key = self._get_key(session, seg.key_uri)
                            data = _aes128_cbc_decrypt(data, key, seg.iv)

                        out_fh.write(data)
                        out_fh.flush()
                        seen_sequences.add(seg.sequence)
                        recorded_bytes += len(data)
                        elapsed = max(time.monotonic() - start_time, 1e-3)
                        self._emit_progress(
                            recorded_bytes, 0, recorded_bytes / elapsed
                        )

                    if playlist.is_endlist:
                        break

                    wait_sec = max(1.0, min(playlist.target_duration * 0.5, 5.0))
                    self._cancel_event.wait(timeout=wait_sec)

            if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                os.replace(part_path, self.output_path)
                self._emit_done(self.output_path)
            else:
                self._emit_fail("Live recording ended with empty output")
        except Exception as exc:
            self._emit_fail(f"Live record error: {exc}")
        finally:
            session.close()


class RetryRunner(threading.Thread):
    """Wraps a task factory with exponential backoff retries."""

    def __init__(
        self,
        task_factory: Callable[..., BaseTask],
        max_retries: int = 3,
        base_delay: float = 1.5,
        max_delay: float = 15.0,
        on_retry: Optional[Callable[[int, int, str], None]] = None,
        on_done: Optional[Callable[[str], None]] = None,
        on_fail: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.task_factory = task_factory
        self.max_retries = max(0, max_retries)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.on_retry = on_retry
        self.on_done = on_done
        self.on_fail = on_fail
        self._cancel_event = threading.Event()
        self._current_task: Optional[BaseTask] = None

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._current_task is not None:
            self._current_task.cancel()

    def _backoff_sleep(self, attempt_idx: int) -> None:
        delay = min(self.base_delay * (2 ** attempt_idx), self.max_delay)
        self._cancel_event.wait(timeout=delay)

    def run(self) -> None:
        total_attempts = self.max_retries + 1
        last_error = "Unknown error"

        for attempt in range(1, total_attempts + 1):
            if self._cancel_event.is_set():
                if self.on_fail:
                    self.on_fail("Cancelled by user")
                return

            state: Dict[str, Optional[str]] = {"done_path": None, "fail_msg": None}

            def _handle_done(path: str, s=state) -> None:
                s["done_path"] = path

            def _handle_fail(msg: str, s=state) -> None:
                s["fail_msg"] = msg

            task = self.task_factory(on_done=_handle_done, on_fail=_handle_fail)
            self._current_task = task
            task.start()
            task.join()

            if state["done_path"] is not None:
                if self.on_done:
                    self.on_done(state["done_path"])
                return

            last_error = state["fail_msg"] or "Task failed"
            if self._cancel_event.is_set() or "Cancelled by user" in last_error:
                if self.on_fail:
                    self.on_fail("Cancelled by user")
                return

            if attempt < total_attempts:
                if self.on_retry:
                    self.on_retry(attempt, self.max_retries, last_error)
                self._backoff_sleep(attempt - 1)

        if self.on_fail:
            self.on_fail(f"Failed after {total_attempts} attempts: {last_error}")
