# -*- coding: utf-8 -*-
"""纯 Python 解析层（无 Qt / 无 Kivy 依赖），供安卓引擎与 UI 复用。

包含：JSON 列表解析（通用 list / ZWMAP groups）、m3u8 解析、AES-128 解密。
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 自定义防盗链请求头：UI 层写入，解析层 / 引擎层读取。
# User-Agent 留空时回退到模块级默认 UA；Referer / Cookie 留空则不带。
CUSTOM_HEADERS: dict[str, str] = {"User-Agent": "", "Referer": "", "Cookie": ""}

# 自定义代理：http://host:port 或 socks5://host:port；留空不走代理。
# SOCKS5 需安装 requests[socks]（pip install requests[socks]）。
CUSTOM_PROXY: str = ""

# 站点请求头预设：一键填入常用 Referer / UA（离线常量，不联网）。
# User-Agent 留空表示沿用当前输入框 / 默认 UA。
HEADER_PRESETS: dict[str, dict[str, str]] = {
    "斗鱼": {"User-Agent": "", "Referer": "https://www.douyu.com/"},
    "抖音": {"User-Agent": "", "Referer": "https://www.douyin.com/"},
    "B站": {"User-Agent": "", "Referer": "https://www.bilibili.com/"},
}


def merged_headers(extra: dict | None = None) -> dict:
    """合并自定义请求头：UA 空用默认 UA，Referer/Cookie 空则省略，extra 追加覆盖。"""
    headers: dict[str, str] = {"User-Agent": CUSTOM_HEADERS.get("User-Agent") or UA}
    referer = CUSTOM_HEADERS.get("Referer") or ""
    if referer:
        headers["Referer"] = referer
    cookie = CUSTOM_HEADERS.get("Cookie") or ""
    if cookie:
        headers["Cookie"] = cookie
    if extra:
        headers.update(extra)
    return headers


def merged_proxies(proxy: str | None = None) -> dict | None:
    """合并代理配置：空返回 None（requests 直连）；否则返回 http/https 代理字典。"""
    p = (proxy or CUSTOM_PROXY or "").strip()
    if not p:
        return None
    return {"http": p, "https": p}

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_OK = True
except ImportError:  # pragma: no cover
    _CRYPTO_OK = False

TOP_LIST_KEYS = ("list", "items", "videos", "data", "entries", "results")


# ---------------- JSON 列表 ----------------
def _resolve(base: str, value: str) -> str:
    if not value:
        return ""
    if value.startswith(("http://", "https://", "//")):
        return value if value.startswith(("http://", "https://")) else "https:" + value
    return urljoin(base, value)


def _first(d: dict, aliases):
    for k in aliases:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return None


def parse_json_text(text: str, base_url: str = "") -> list[dict]:
    """返回 [{name, detail, url, cover, is_live, group}]，失败返回 []。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    rows: list[dict] = []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        groups = payload.get("groups")
        if isinstance(groups, list):
            for g in groups:
                if not isinstance(g, dict):
                    continue
                gname = g.get("name") or g.get("id") or ""
                for it in g.get("items") or []:
                    if isinstance(it, dict):
                        it.setdefault("__group__", gname)
                        rows.append(it)
        if not rows:
            for key in TOP_LIST_KEYS:
                val = payload.get(key)
                if isinstance(val, list):
                    rows = [x for x in val if isinstance(x, dict)]
                    break
        if not rows:
            rows = [payload]

    out: list[dict] = []
    for row in rows:
        name = str(_first(row, ("name", "anchor", "title", "subject")) or "未命名")
        detail = str(row.get("title") or row.get("desc") or "")
        url = _first(row, ("url", "play_url", "video_url", "play", "stream_url",
                           "src", "m3u8", "mp4", "download_url", "download"))
        cover = _resolve(base_url, str(_first(row, ("poster", "cover", "thumb",
                                                    "thumbnail", "img", "image")) or ""))
        is_live = bool(row.get("isLive") or row.get("is_live"))
        group = str(row.get("__group__") or row.get("group") or "")
        url = _resolve(base_url, str(url or ""))
        if url:
            out.append({"name": name, "detail": detail, "url": url,
                        "cover": cover, "is_live": is_live, "group": group})
    return out


def load_json_url(url: str, timeout: int = 15, headers: dict | None = None,
                  proxies: dict | None = None) -> list[dict]:
    h = merged_headers(headers)
    h.setdefault("Referer", url)
    r = requests.get(url, headers=h, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return parse_json_text(r.text, base_url=url)


# ---------------- m3u8 ----------------
def fetch_text(url: str, headers: dict | None = None,
               proxies: dict | None = None) -> str:
    r = requests.get(url, headers=merged_headers(headers), timeout=20,
                     proxies=proxies)
    r.raise_for_status()
    return r.text


def list_variants(master_text: str, base: str) -> list[dict]:
    """解析 master m3u8 的 #EXT-X-STREAM-INF，按带宽降序返回清晰度列表。

    每项 {"bandwidth", "resolution", "url", "label"}；label 优先用分辨率，
    无分辨率时用码率近似标注。
    """
    variants: list[dict] = []
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m_bw = re.search(r"BANDWIDTH=(\d+)", line)
            m_res = re.search(r"RESOLUTION=([0-9x]+)", line)
            bw = int(m_bw.group(1)) if m_bw else 0
            res = m_res.group(1) if m_res else ""
            for nxt in lines[i + 1: i + 3]:
                nxt = nxt.strip()
                if nxt and not nxt.startswith("#"):
                    variants.append({
                        "bandwidth": bw,
                        "resolution": res,
                        "url": urljoin(base, nxt),
                        "label": res or f"{bw // 1000}kbps",
                    })
                    break
    variants.sort(key=lambda v: v["bandwidth"], reverse=True)
    return variants


def resolve_variant(master_text: str, base: str) -> str | None:
    best_bw, best_url = -1, None
    lines = master_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
            for nxt in lines[i + 1: i + 3]:
                nxt = nxt.strip()
                if nxt and not nxt.startswith("#"):
                    if bw > best_bw:
                        best_bw, best_url = bw, urljoin(base, nxt)
                    break
    return best_url


def parse_media(text: str, base: str):
    """返回 (分片绝对URL列表, key信息, 是否点播含ENDLIST)。"""
    segments: list[str] = []
    key_info: dict | None = None
    ended = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            method = re.search(r'METHOD=([A-Z0-9-]+)', line)
            uri = re.search(r'URI="([^"]+)"', line)
            iv = re.search(r'IV=0x([0-9A-Fa-f]+)', line)
            if method and method.group(1) == "AES-128" and uri:
                key_info = {"uri": urljoin(base, uri.group(1)),
                            "iv": bytes.fromhex(iv.group(1)) if iv else None}
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#"):
            continue
        elif line:
            segments.append(urljoin(base, line))
    return segments, key_info, ended


def local_cover_or_url(cover_url: str, cache_dir: str) -> str:
    """有本地缓存返回文件路径，否则返回原 URL（不发请求，供 UI 安全调用）。"""
    if not cover_url:
        return ""
    p = cover_cache_path(cover_url, cache_dir)
    if os.path.exists(p):
        return p
    return cover_url


def cover_cache_path(cover_url: str, cache_dir: str) -> str:
    """封面URL对应的缓存文件路径（URL hash 命名，避免非法字符）。"""
    import hashlib
    h = hashlib.md5(cover_url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, h + ".img")


def cached_cover_path(cover_url: str, cache_dir: str, timeout: int = 10) -> str:
    """封面落盘缓存：命中直接返回本地路径；否则下载入库后返回；失败返回原 URL。

    仅在后台线程调用（会阻塞等待网络）。
    """
    if not cover_url or not cover_url.startswith(("http://", "https://")):
        return cover_url or ""
    p = cover_cache_path(cover_url, cache_dir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        if not os.path.exists(p):
            r = requests.get(cover_url, headers=merged_headers(),
                             proxies=merged_proxies(), timeout=timeout)
            r.raise_for_status()
            with open(p, "wb") as f:
                f.write(r.content)
        return p
    except Exception:  # noqa: BLE001
        return cover_url


def aes_decrypt(data: bytes, key: bytes, iv: bytes | None, seq: int) -> bytes:
    if iv is None:
        iv = seq.to_bytes(16, "big")
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    plain = dec.update(data) + dec.finalize()
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    return plain


def kind_of(url: str) -> str:
    low = url.lower().split("?")[0].split("#")[0]
    if ".m3u8" in low:
        return "hls"
    if ".mp4" in low or ".webm" in low:
        return "mp4"
    return "unknown"


def safe_filename(name: str) -> str:
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip().strip(".") or "video"


def apply_filename_template(template: str, name: str, time_str: str,
                            quality: str = "", title: str = "") -> str:
    """按模板生成安全文件名（不含扩展名）。

    占位符：{name} 主播名、{title} 标题、{time} 时间、{quality} 清晰度；
    模板为空时回退原名；最后统一过 safe_filename 清洗。
    """
    t = (template or "").strip()
    if not t:
        base = name or title or "video"
    else:
        base = (t.replace("{name}", name)
                 .replace("{title}", title or name)
                 .replace("{time}", time_str)
                 .replace("{quality}", quality or ""))
    return safe_filename(base)
