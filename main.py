# -*- coding: utf-8 -*-
"""Kivy 安卓 App：JSON 列表 / 自定义地址 播放、下载、直播录制 + 监控关注列表。

在 Termux / 桌面可直接 `python main.py` 预览；正式 APK 用 buildozer 构建。
播放：安卓端通过系统 Intent 调起外部播放器（VLC/系统播放器）打开 URL 或文件。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.widget import Widget

from parser import (load_json_url, kind_of, CUSTOM_HEADERS, HEADER_PRESETS,
                    local_cover_or_url, cached_cover_path)
from engine import build_task
from monitor import MonitorPoller

# 磁盘剩余空间低于该阈值（字节）时跳过入队：100MB
MIN_FREE_BYTES = 100 * 1024 * 1024

# 深色底色（无头/CI 等窗口不可用环境下自动跳过，不影响安卓真机）
try:
    Window.clearcolor = (0.1, 0.11, 0.13, 1)
except Exception:  # noqa: BLE001
    pass


def save_dir() -> str:
    """优先安卓公共 Movies 目录，失败回退当前目录下 downloads。"""
    candidates = [
        "/storage/emulated/0/Movies",
        "/sdcard/Movies",
        os.path.join(os.path.expanduser("~"), "Downloads"),
        "downloads",
    ]
    for c in candidates:
        try:
            os.makedirs(c, exist_ok=True)
            return c
        except OSError:
            continue
    return "downloads"


def open_external(url: str, is_file: bool = False):
    """安卓：调起外部播放器；非安卓仅提示。"""
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        if is_file:
            # pyjnius 导入 Java 类必须用 autoclass，不存在 `from java.io import File` 语法
            File = autoclass("java.io.File")
            uri = Uri.fromFile(File(url))
        else:
            uri = Uri.parse(url)
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(uri, "video/*")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        activity.startActivity(intent)
    except Exception as e:  # noqa: BLE001
        return f"无法调起播放器：{e}"
    return ""


# ---------------- 内置播放器（安卓 VideoView） ----------------
_video_view = None


def play_in_app(url: str):
    """应用内播放：Android 系统 VideoView 叠加在 Kivy 界面上方。

    MediaPlayer 后端支持 MP4 与多数 HLS(m3u8)；触摸显示系统控制条（暂停/进度）。
    返回错误信息或空串（成功）。
    """
    global _video_view
    try:
        from jnius import autoclass
        Uri = autoclass("android.net.Uri")
        VideoView = autoclass("android.widget.VideoView")
        MediaController = autoclass("android.widget.MediaController")
        ViewGroup = autoclass("android.view.ViewGroup")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        stop_in_app_player()  # 已在播则先移除，避免叠两层
        view = VideoView(activity)
        lp = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                                    ViewGroup.LayoutParams.MATCH_PARENT)
        view.setVideoURI(Uri.parse(url))
        view.setMediaController(MediaController(activity))
        activity.addContentView(view, lp)
        view.start()
        _video_view = view
    except Exception as e:  # noqa: BLE001
        return f"内置播放不可用：{e}"
    return ""


def stop_in_app_player():
    """移除内置播放器视图，回到应用界面。"""
    global _video_view
    if _video_view is None:
        return
    try:
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        activity.removeContentView(_video_view)
    except Exception:  # noqa: BLE001
        pass
    _video_view = None


class Row(BoxLayout):
    """列表里的一行。"""
    def __init__(self, item, on_play, on_dl, on_star=None, starred=False, **kw):
        super().__init__(**kw)
        self.orientation = "horizontal"
        self.size_hint_y = None
        self.height = 64
        self.item = item
        info = BoxLayout(orientation="vertical", size_hint_x=0.66)
        title = item.get("name", "未命名")
        if starred:
            title = "★ " + title
        if item.get("group"):
            title = f"[{item['group']}] {title}"
        info.add_widget(Label(text=title, halign="left", valign="middle",
                               size_hint_y=0.6, text_size=(None, None), color=(1, 1, 1, 1)))
        detail = item.get("detail") or item.get("url", "")
        info.add_widget(Label(text=detail[:60], halign="left", valign="middle",
                               size_hint_y=0.4, font_size=11, color=(0.7, 0.7, 0.7, 1),
                               text_size=(None, None)))
        self.add_widget(info)
        b_star = Button(text="★" if starred else "☆", size_hint_x=0.11)
        if on_star:
            b_star.bind(on_release=lambda *_: on_star(item))
        b_play = Button(text="播", size_hint_x=0.11)
        b_play.bind(on_release=lambda *_: on_play(item))
        b_dl = Button(text="下载", size_hint_x=0.12)
        b_dl.bind(on_release=lambda *_: on_dl(item))
        self.add_widget(b_star)
        self.add_widget(b_play)
        self.add_widget(b_dl)


class Card(BoxLayout):
    """图文网格里的一张封面卡片（封面优先用本地缓存，无缓存回退在线 URL）。"""
    def __init__(self, item, on_play, on_dl, on_star=None, starred=False, **kw):
        super().__init__(orientation="vertical", size_hint_y=None, height=175, **kw)
        cover = item.get("cover")
        if cover:
            src = local_cover_or_url(cover, os.path.join(save_dir(), "covers"))
            self.add_widget(AsyncImage(source=src, size_hint_y=0.66,
                                       allow_stretch=True, keep_ratio=False))
        else:
            self.add_widget(Label(text="无封面", size_hint_y=0.66,
                                  color=(0.5, 0.5, 0.5, 1)))
        name = ("★ " if starred else "") + item.get("name", "未命名")
        self.add_widget(Label(text=name, size_hint_y=0.13, halign="center",
                              valign="middle", text_size=(None, None), color=(1, 1, 1, 1)))
        btns = BoxLayout(orientation="horizontal", size_hint_y=0.15, spacing=4)
        bs = Button(text="★" if starred else "☆")
        if on_star:
            bs.bind(on_release=lambda *_: on_star(item))
        bp = Button(text="播")
        bd = Button(text="下载")
        bp.bind(on_release=lambda *_: on_play(item))
        bd.bind(on_release=lambda *_: on_dl(item))
        btns.add_widget(bs)
        btns.add_widget(bp)
        btns.add_widget(bd)
        self.add_widget(btns)


class TagRow(BoxLayout):
    """可删除的源/关注词一行。"""
    def __init__(self, text, on_del, **kw):
        super().__init__(**kw)
        self.orientation = "horizontal"
        self.size_hint_y = None
        self.height = 34
        self.add_widget(Label(text=text, halign="left", valign="middle",
                              size_hint_x=0.85, text_size=(None, None), color=(1, 1, 1, 1)))
        b = Button(text="删", size_hint_x=0.15, font_size=11)
        b.bind(on_release=lambda *_: on_del(self))
        self.add_widget(b)


class DrawerCard(BoxLayout):
    """抽屉菜单里的一个分区卡片：标题 + 内容。"""
    def __init__(self, title: str, **kw):
        super().__init__(orientation="vertical", size_hint_y=None, spacing=6, padding=8, **kw)
        self.add_widget(Label(text=title, size_hint_y=None, height=24, bold=True,
                              halign="left", color=(0.35, 0.4, 0.5, 1)))


class WaterfallCard(BoxLayout):
    """瀑布流卡片（仿小红书式双列卡片）：大封面 + 底部信息条。

    整卡按下弹出操作菜单（播放/下载/星标），不再放小按钮。
    """

    def __init__(self, item, on_open, on_star=None, starred=False, **kw):
        super().__init__(orientation="vertical", size_hint_y=None, **kw)
        self.item = item
        self.padding = 0
        self.spacing = 0
        cover = item.get("cover")
        name = ("★ " if starred else "") + item.get("name", "未命名")
        if cover:
            src = local_cover_or_url(cover, os.path.join(save_dir(), "covers"))
            self.img = AsyncImage(source=src, size_hint_y=None, allow_stretch=True,
                                  keep_ratio=False)
        else:
            self.img = None
            self.add_widget(Label(text="无封面", size_hint_y=None, height=150,
                                  color=(0.6, 0.6, 0.6, 1)))
        if self.img is not None:
            self.add_widget(self.img)
        # 信息条：名称 + 星标状态
        bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=36,
                        padding=[6, 2, 6, 2])
        bar.add_widget(Label(text=name, halign="left", valign="middle",
                             text_size=(None, None), color=(0.15, 0.15, 0.15, 1)))
        self.add_widget(bar)
        self.bind(on_touch_down=self._on_touch)
        self._on_open = on_open

    def _on_touch(self, _w, touch):
        if self.collide_point(*touch.pos):
            self._on_open(self.item)
            return True
        return False


class MainApp(App):
    def build(self):
        self.items: list[dict] = []
        self.tasks: dict[str, object] = {}
        self.sources: list[tuple[str, bool]] = []
        self.follows: list[str] = []
        self.seen: set[str] = set()
        self.poller: MonitorPoller | None = None
        self._refresh_event = None
        # 下载记录与请求头 / 重试等设置
        self.history: list[dict] = []
        self.stars: set[str] = set()
        self.settings: dict[str, str] = {
            "ua": "", "referer": "", "cookie": "",
            "max_retries": "3", "segment_minutes": "0",
            "proxy": "", "speed_limit": "0", "name_template": "",
            "max_reconnect": "5",
        }
        self._history_visible = False
        # 追加式日志缓存（_log 维护，最多保留 60 条）
        self.log_lines: list[str] = []
        # 任务去重：同地址正在队列中则不再入队
        self._active_urls: set[str] = set()
        # 录制中任务计数（持 WakeLock 期间不清零）
        self._recording = 0
        self._wl = None
        # 封面预热：本轮会话已尝试过的封面 URL（失败仅试一次，防循环）
        self._cover_attempted: set[str] = set()
        # 下拉刷新防抖：上次触发时刻
        self._last_pull = 0.0

        # ============ 整体框架：顶栏 + 内容区 + 底部状态条 ============
        app_root = BoxLayout(orientation="vertical")

        # ---- 顶栏：汉堡菜单 | 品牌 Tab | 搜索 ----
        topbar = BoxLayout(orientation="horizontal", size_hint_y=None, height=48,
                           padding=[10, 4, 10, 4], spacing=8)
        b_menu = Button(text="☰", font_size=22, size_hint_x=None, width=44,
                        background_normal="", background_color=(1, 1, 1, 1))
        b_menu.bind(on_release=lambda *_: self._toggle_drawer())
        topbar.add_widget(b_menu)

        self.platform_tabs = BoxLayout(orientation="horizontal", spacing=18,
                                       size_hint_x=0.6, padding=[4, 4, 4, 0])
        self._tab_buttons: list[Button] = []
        self._build_platform_tabs([])
        topbar.add_widget(self.platform_tabs)

        b_search = Button(text="🔍", font_size=18, size_hint_x=None, width=44,
                          background_normal="", background_color=(1, 1, 1, 1))
        b_search.bind(on_release=lambda *_: self._toggle_search())
        topbar.add_widget(b_search)
        app_root.add_widget(topbar)

        # ---- 搜索条（默认隐藏，点🔍显示） ----
        self.search_bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=40,
                                    padding=[10, 2, 10, 2], spacing=6)
        self.search_input = TextInput(hint_text="搜索名称 / 详情…", multiline=False,
                                      size_hint_x=0.8, background_normal="",
                                      background_color=(0.94, 0.94, 0.96, 1))
        self.search_input.bind(text=lambda *_: self._apply_filter())
        b_close_s = Button(text="✕", size_hint_x=None, width=40,
                           background_normal="", background_color=(0.94, 0.94, 0.96, 1))
        b_close_s.bind(on_release=lambda *_: self._toggle_search(force_close=True))
        self.search_bar.add_widget(self.search_input)
        self.search_bar.add_widget(b_close_s)
        self.search_bar.height = 0
        self.search_bar.size_hint_y = None
        self.search_bar.disabled = True
        app_root.add_widget(self.search_bar)

        # ---- 平台信息条（当前选中平台 + 条数，仿抖音频道提示） ----
        self.platform_hint = Label(text="全部 · 拉取列表后点击卡片操作", size_hint_y=None,
                                   height=26, halign="left", padding=[12, 0, 12, 0],
                                   color=(0.45, 0.47, 0.5, 1), font_size=12)
        app_root.add_widget(self.platform_hint)

        # ---- 内容区：双列瀑布流（Kivy 无原生瀑布流，用等宽双列近似） ----
        self.grid_scroll = ScrollView()
        col_wrap = BoxLayout(orientation="horizontal", spacing=8, padding=[8, 4, 8, 4],
                             size_hint_y=None)
        col_wrap.bind(minimum_height=col_wrap.setter("height"))
        self.grid_layout = GridLayout(cols=2, size_hint_y=None, spacing=8)
        self.grid_layout.bind(minimum_height=self.grid_layout.setter("height"))
        # 列表视图（保留旧行式列表，放抽屉里可切换）
        self.list_view = ScrollView()
        self.list_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=4)
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.list_view.add_widget(self.list_box)
        col_wrap.add_widget(self.grid_layout)
        self.grid_scroll.add_widget(col_wrap)
        # 下拉刷新：顶部过度下拉后松手即重新拉取 JSON（两种视图均支持）
        self.grid_scroll.bind(on_scroll_stop=self._on_pull_refresh)
        self.list_view.bind(on_scroll_stop=self._on_pull_refresh)
        self._view = 1  # 默认瀑布流卡片视图
        self.view_stack_holder = BoxLayout()
        self.view_stack_holder.add_widget(self.grid_scroll)
        app_root.add_widget(self.view_stack_holder)

        # ---- 底部状态条：统计 | 日志尾部 | 关播放器 ----
        bottom = BoxLayout(orientation="horizontal", size_hint_y=None, height=34,
                           padding=[8, 2, 8, 2], spacing=6)
        self.stats_label = Label(text="统计：暂无", size_hint_x=0.35, halign="left",
                                 color=(0.35, 0.5, 0.8, 1), font_size=12,
                                 text_size=(None, None))
        self.log_tail = Label(text="就绪", size_hint_x=0.45, halign="left",
                              color=(0.55, 0.65, 0.55, 1), font_size=11,
                              text_size=(None, None))
        b_close_pl = Button(text="关播放器", size_hint_x=0.2, font_size=12,
                            background_normal="", background_color=(0.94, 0.94, 0.96, 1))
        b_close_pl.bind(on_release=lambda *_: self._close_player())
        bottom.add_widget(self.stats_label)
        bottom.add_widget(self.log_tail)
        bottom.add_widget(b_close_pl)
        app_root.add_widget(bottom)

        # 把旧 Log 主控件指向底部小控件：保留全部日志逻辑（追加 60 条）
        self.log = self.log_tail

        # ============ 左侧抽屉菜单：全部配置/监控/收藏/记录 ============
        self.drawer = ScrollView(size_hint_x=None, width=0)
        self.drawer_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=8,
                                    padding=10)   # 原为 padding=[10,10,10,10] 无效写法，双元素即水平垂直
        self.drawer_box.bind(minimum_height=self.drawer_box.setter("height"))
        self.drawer.add_widget(self.drawer_box)

        # --- 抽屉分区 1：数据源 ---
        c1 = DrawerCard("数据源")
        jrow = BoxLayout(orientation="horizontal", size_hint_y=None, height=38, spacing=4)
        self.json_input = TextInput(hint_text="JSON 列表 URL", multiline=False, size_hint_x=0.7)
        b_load = Button(text="加载", size_hint_x=0.3)
        b_load.bind(on_release=self.load_json)
        jrow.add_widget(self.json_input)
        jrow.add_widget(b_load)
        c1.add_widget(jrow)
        rrow = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=4)
        self.auto_refresh = CheckBox(size_hint_x=0.12)
        self.auto_refresh.bind(active=self._toggle_auto_refresh)
        rrow.add_widget(Label(text="定时刷新(秒)", size_hint_x=0.42, halign="left"))
        self.refresh_sec = TextInput(text="60", multiline=False, size_hint_x=0.2,
                                     input_filter="int")
        rrow.add_widget(self.refresh_sec)
        b_refresh = Button(text="刷新", size_hint_x=0.26)
        b_refresh.bind(on_release=lambda *_: self.refresh_list())
        rrow.add_widget(self.auto_refresh)
        rrow.add_widget(b_refresh)
        c1.add_widget(rrow)
        # 列表/图文切换
        vrow = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=4)
        b_vlist = Button(text="行式列表", size_hint_x=0.5)
        b_vgrid = Button(text="卡片瀑布", size_hint_x=0.5)
        b_vlist.bind(on_release=lambda *_: (self._switch_view(0), self._toggle_drawer()))
        b_vgrid.bind(on_release=lambda *_: (self._switch_view(1), self._toggle_drawer()))
        vrow.add_widget(b_vlist)
        vrow.add_widget(b_vgrid)
        c1.add_widget(vrow)
        self.drawer_box.add_widget(c1)

        # --- 抽屉分区 2：监控 / 关注 ---
        c2 = DrawerCard("监控 · 关注列表")
        src_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, spacing=4)
        self.src_input = TextInput(hint_text="监控源 JSON URL", multiline=False, size_hint_x=0.66)
        b_src_add = Button(text="加源", size_hint_x=0.17)
        b_src_clr = Button(text="清空", size_hint_x=0.17)
        b_src_add.bind(on_release=lambda *_: self._add_source())
        b_src_clr.bind(on_release=lambda *_: self._clear_sources())
        src_row.add_widget(self.src_input)
        src_row.add_widget(b_src_add)
        src_row.add_widget(b_src_clr)
        c2.add_widget(src_row)
        self.src_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=2)
        self.src_box.bind(minimum_height=self.src_box.setter("height"))
        c2.add_widget(self.src_box)
        fol_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, spacing=4)
        self.fol_input = TextInput(hint_text="关注名称/关键词", multiline=False, size_hint_x=0.66)
        b_fol_add = Button(text="加关注", size_hint_x=0.17)
        b_fol_clr = Button(text="清空", size_hint_x=0.17)
        b_fol_add.bind(on_release=lambda *_: self._add_follow())
        b_fol_clr.bind(on_release=lambda *_: self._clear_follows())
        fol_row.add_widget(self.fol_input)
        fol_row.add_widget(b_fol_add)
        fol_row.add_widget(b_fol_clr)
        c2.add_widget(fol_row)
        self.fol_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=2)
        self.fol_box.bind(minimum_height=self.fol_box.setter("height"))
        c2.add_widget(self.fol_box)
        opt_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, spacing=4)
        opt_row.add_widget(Label(text="间隔(秒):", size_hint_x=0.25, halign="left"))
        self.interval_input = TextInput(text="30", multiline=False, size_hint_x=0.18, input_filter="int")
        self.auto_dl = CheckBox(size_hint_x=0.1)
        opt_row.add_widget(self.interval_input)
        opt_row.add_widget(self.auto_dl)
        opt_row.add_widget(Label(text="命中自动下载", size_hint_x=0.4, halign="left"))
        b_start = Button(text="开始", size_hint_x=0.14)
        b_stop = Button(text="停止", size_hint_x=0.13)
        b_start.bind(on_release=lambda *_: self._start_monitor())
        b_stop.bind(on_release=lambda *_: self._stop_monitor())
        opt_row.add_widget(b_start)
        opt_row.add_widget(b_stop)
        c2.add_widget(opt_row)
        self.drawer_box.add_widget(c2)

        # --- 抽屉分区 3：请求头 / 代理 / 下载参数 ---
        c3 = DrawerCard("下载 / 录制参数")
        cu = BoxLayout(orientation="horizontal", size_hint_y=None, height=38, spacing=4)
        self.url_input = TextInput(hint_text="自定义直链 .mp4/.m3u8/flv", multiline=False,
                                   size_hint_x=0.62)
        self.type_spinner = Spinner(text="自动识别", values=["自动识别", "MP4 点播", "HLS 直播"],
                                    size_hint_x=0.2)
        b_cgo = Button(text="播放", size_hint_x=0.18)
        b_cgo.bind(on_release=lambda *_: self.custom_play())
        cu.add_widget(self.url_input)
        cu.add_widget(self.type_spinner)
        cu.add_widget(b_cgo)
        c3.add_widget(cu)
        cu2 = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        b_cext = Button(text="外部播放", size_hint_x=0.25)
        b_cdl = Button(text="下载", size_hint_x=0.25)
        b_crec = Button(text="录制", size_hint_x=0.25)
        b_ext2 = Button(text="停止任务", size_hint_x=0.25)
        b_cext.bind(on_release=lambda *_: self._play_external(self.url_input.text.strip()))
        b_cdl.bind(on_release=lambda *_: self.custom_download())
        b_crec.bind(on_release=lambda *_: self.custom_record())
        b_ext2.bind(on_release=lambda *_: self._stop_all_tasks())
        cu2.add_widget(b_cext)
        cu2.add_widget(b_cdl)
        cu2.add_widget(b_crec)
        cu2.add_widget(b_ext2)
        c3.add_widget(cu2)
        row_t = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_t.add_widget(Label(text="定时(分):", size_hint_x=0.25, halign="left"))
        self.min_input = TextInput(text="0", multiline=False, size_hint_x=0.2, input_filter="int")
        row_t.add_widget(self.min_input)
        row_t.add_widget(Label(text="分段(分):", size_hint_x=0.25, halign="left"))
        self.seg_input = TextInput(text="0", multiline=False, size_hint_x=0.2, input_filter="int")
        row_t.add_widget(self.seg_input)
        c3.add_widget(row_t)
        row_opt = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_opt.add_widget(Label(text="重试:", size_hint_x=0.2, halign="left"))
        self.retry_input = TextInput(text="3", multiline=False, size_hint_x=0.2, input_filter="int")
        row_opt.add_widget(Label(text="重连:", size_hint_x=0.2, halign="left"))
        self.reconnect_input = TextInput(text="5", multiline=False, size_hint_x=0.2, input_filter="int")
        row_opt.add_widget(Label(text="限速KB/s:", size_hint_x=0.24, halign="left"))
        self.speed_input = TextInput(text="0", multiline=False, size_hint_x=0.16, input_filter="int")
        row_opt.add_widget(self.retry_input)
        row_opt.add_widget(self.reconnect_input)
        row_opt.add_widget(self.speed_input)
        c3.add_widget(row_opt)
        row_h1 = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_h1.add_widget(Label(text="User-Agent:", size_hint_x=0.3, halign="left"))
        self.ua_input = TextInput(hint_text="留空用默认 UA", multiline=False, size_hint_x=0.7)
        row_h1.add_widget(self.ua_input)
        c3.add_widget(row_h1)
        row_h2 = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_h2.add_widget(Label(text="Referer:", size_hint_x=0.3, halign="left"))
        self.referer_input = TextInput(hint_text="留空不带", multiline=False, size_hint_x=0.7)
        row_h2.add_widget(self.referer_input)
        c3.add_widget(row_h2)
        row_h3 = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_h3.add_widget(Label(text="Cookie:", size_hint_x=0.3, halign="left"))
        self.cookie_input = TextInput(hint_text="留空不带", multiline=False, size_hint_x=0.7)
        row_h3.add_widget(self.cookie_input)
        c3.add_widget(row_h3)
        row_p = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_p.add_widget(Label(text="代理:", size_hint_x=0.22, halign="left"))
        self.proxy_input = TextInput(hint_text="http:// 或 socks5://host:port",
                                     multiline=False, size_hint_x=0.78)
        row_p.add_widget(self.proxy_input)
        c3.add_widget(row_p)
        row_tpl = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_tpl.add_widget(Label(text="命名模板:", size_hint_x=0.28, halign="len"))
        row_tpl.add_widget(Label(text="", size_hint_x=0.28))
        row_tpl.add_widget(Label(text="模板:", size_hint_x=0.28))
        row_tpl.add_widget(Label(text="", size_hint_x=0.16))
        # 占位防误触；实际输入框放下一行（避免与模板标签同行拥挤）
        row_tpl2 = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4)
        row_tpl2.add_widget(Label(text="{name} {time} {title} {quality}", size_hint_x=0.42,
                                  halign="left", font_size=10, color=(0.6, 0.6, 0.6, 1)))
        self.tpl_input = TextInput(hint_text="留空用原名", multiline=False, size_hint_x=0.58)
        row_tpl2.add_widget(self.tpl_input)
        c3.add_widget(row_p)
        c3.add_widget(row_tpl2)
        self.drawer_box.add_widget(c3)

        # --- 抽屉分区 4：站点预设 + 收藏/记录/日志导出 ---
        c4 = DrawerCard("预设 / 收藏 / 记录")
        preset_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=4)
        for name in HEADER_PRESETS:
            b = Button(text=name, size_hint_x=0.33)
            b.bind(on_release=lambda *_, n=name: self._apply_preset(n))
            preset_row.add_widget(b)
        c4.add_widget(preset_row)
        act_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=4)
        b_exp_fav = Button(text="导出收藏", size_hint_x=0.25)
        b_imp_fav = Button(text="导入收藏", size_hint_x=0.25)
        b_exp_m3u = Button(text="导出M3U", size_hint_x=0.25)
        b_exp_log = Button(text="导出日志", size_hint_x=0.25)
        b_exp_fav.bind(on_release=lambda *_: self._export_favorites())
        b_imp_fav.bind(on_release=lambda *_: self._import_favorites())
        b_exp_m3u.bind(on_release=lambda *_: self._export_m3u())
        b_exp_log.bind(on_release=lambda *_: self._export_log())
        act_row.add_widget(b_exp_fav)
        act_row.add_widget(b_imp_fav)
        act_row.add_widget(b_exp_m3u)
        act_row.add_widget(b_exp_log)
        c4.add_widget(act_row)
        row_hist = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=4)
        b_hist = Button(text="下载记录", size_hint_x=0.5)
        b_hist.bind(on_release=lambda *_: self._toggle_history())
        row_hist.add_widget(b_hist)
        row_hist.add_widget(Widget(size_hint_x=0.5))
        c4.add_widget(row_hist)
        self.history_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=2)
        self.history_box.bind(minimum_height=self.history_box.setter("height"))
        c4.add_widget(self.history_box)
        self.drawer_box.add_widget(c4)

        # ---- 组装：主界面 + 抽屉层叠 ----
        from kivy.uix.relativelayout import RelativeLayout
        layer = RelativeLayout()
        layer.add_widget(app_root)
        layer.add_widget(self.drawer)
        self._drawer_open = False

        self._load_state()
        return layer

    # ---------- 工具 ----------
    def _toggle_drawer(self, *_):
        """左侧抽屉菜单开合（含监控/参数/收藏全部配置）。"""
        self._drawer_open = not self._drawer_open
        self.drawer.width = min(340, Window.width * 0.86) if self._drawer_open else 0

    def _toggle_search(self, *_, force_close: bool = False):
        """搜索条展开/收起（顶栏🔍）。"""
        opened = self.search_bar.height > 0
        if force_close or opened:
            self.search_bar.height = 0
            self.search_bar.disabled = True
        else:
            self.search_bar.height = 40
            self.search_bar.disabled = False
            self.search_input.focus = True

    def _build_platform_tabs(self, groups: list[str]):
        """构建顶部品牌 Tab（含「全部」），点选按平台过滤卡片。"""
        self.platform_tabs.clear_widgets()
        self._tab_buttons = []
        names = ["全部"] + [g for g in groups if g]
        for gname in names:
            b = Button(text=gname, font_size=17, bold=True,
                       background_normal="", background_color=(1, 1, 1, 1),
                       color=(0.05, 0.05, 0.05, 1), size_hint_x=None,
                       width=max(52, 22 * len(gname)))
            b.bind(on_release=lambda *_
, n=gname: self._select_platform(n))  # noqa: E128 换行防长行，等价单行
            self.platform_tabs.add_widget(b)
            self._tab_buttons.append((gname, b))
        self._platform = "全部"
        self._refresh_tab_states()

    def _refresh_tab_states(self):
        for gname, b in self._tab_buttons:
            b.bold = (gname == self._platform)
            b.color = (0.05, 0.05, 0.05, 1) if gname == self._platform else (0.55, 0.55, 0.58, 1)
            b.text = (gname + " ▾") if gname == self._platform else gname

    def _select_platform(self, gname: str):
        self._platform = gname
        self._refresh_tab_states()
        self._apply_filter()

    def _log(self, msg: str):
        """追加式日志：保留最近 60 条，避免进度覆盖历史事件。"""
        def cb(_dt):
            self.log_lines.append(msg)
            if len(self.log_lines) > 60:
                del self.log_lines[: len(self.log_lines) - 60]
            self.log.text = "\n".join(self.log_lines)
        Clock.schedule_once(cb)

    def _stop_all_tasks(self, *_):
        """停止全部下载/录制任务（引擎 stop 会级联停重试包装与内层任务）。"""
        n = len(self.tasks)
        for t in list(self.tasks.values()):
            try:
                t.stop()
            except Exception:  # noqa: BLE001
                pass
        self.tasks.clear()
        self._active_urls.clear()
        self._recording = 0
        self._wake_lock(False)
        self._log(f"已请求停止全部任务（{n} 个）")

    def _switch_view(self, idx: int):
        self._view = idx
        self.view_stack_holder.clear_widgets()
        self.view_stack_holder.add_widget(self.grid_scroll if idx == 1 else self.list_view)

    def _on_pull_refresh(self, scroll, touch):
        """顶部过度下拉后松手 → 刷新当前 JSON 列表（2 秒防抖）。"""
        try:
            if scroll.scroll_y <= 1.03:
                return
            now = time.time()
            if now - self._last_pull < 2.0:
                return
            self._last_pull = now
            url = (self.json_input.text or "").strip()
            if not url:
                self.platform_hint.text = "未配置列表地址 · 左上角菜单里填写 JSON URL"
                self._log("未配置 JSON 地址，无法下拉刷新")
                return
            self.platform_hint.text = "↓ 下拉刷新中…"
            self._log("↓ 下拉刷新列表")
            self.refresh_list()
        except Exception:  # noqa: BLE001
            pass

    # ---------- JSON 列表 ----------
    def load_json(self, *_):
        self.fetch_list(self.json_input.text.strip(), announce=True)
        # 加载时收抽屉，回主界面看结果
        if getattr(self, "_drawer_open", False):
            self._toggle_drawer()

    def refresh_list(self, *_):
        self.fetch_list(self.json_input.text.strip(), announce=False)

    def _refresh_platforms(self):
        """按当前条目重建顶部平台 Tab。"""
        groups = sorted({it.get("group", "") for it in self.items})
        kept = getattr(self, "_platform", "全部")
        if kept not in ["全部"] + [g for g in groups if g]:
            kept = "全部"
        self._build_platform_tabs([g for g in groups if g])
        self._platform = kept
        self._refresh_tab_states()
        # 信息条提示
        if self.items:
            self.platform_hint.text = (f"{self._platform} · {len(self._visible_items())} 条"
                                       + " · 点击卡片出操作")
        else:
            self.platform_hint.text = "全部 · 左上角菜单加载列表"

    def fetch_list(self, url: str, announce: bool):
        """后台线程拉取 JSON，避免网络阻塞 UI 线程（安卓主线程阻塞会触发 ANR）。"""
        if not url:
            self._log("请输入 JSON URL")
            return
        # 持久化列表地址：重启自动回填（点加载即记住，含失败重试场景）
        if self.settings.get("json_url") != url:
            self.settings["json_url"] = url
            self._save_state()
        self._log("加载中…" if announce else "刷新中…")

        def work():
            try:
                items = load_json_url(url)
            except Exception as e:  # noqa: BLE001
                self._log(f"刷新失败：{e}")
                return

            def cb(_dt):
                self.items = items
                self._refresh_platforms()
                self._rebuild_list()
                self._log(f"加载 {len(items)} 条" if announce else f"已刷新 {len(items)} 条")
            Clock.schedule_once(cb)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 搜索 / 星标 ----------
    def _visible_items(self) -> list[dict]:
        """搜索词 + 平台 Tab 双重过滤；星标置顶。"""
        kw = (self.search_input.text or "").strip().lower()
        platform = getattr(self, "_platform", "全部")

        def hit(it: dict) -> bool:
            if kw and not (kw in (it.get("name", "") or "").lower()
                           or kw in (it.get("detail", "") or "").lower()):
                return False
            if platform != "全部" and (it.get("group", "") or "") != platform:
                return False
            return True

        items = [it for it in self.items if hit(it)]
        return sorted(items, key=lambda it: it.get("url", "") not in self.stars)

    def _apply_filter(self, *_):
        """搜索词/平台Tab变化后重建视图并更新信息条。"""
        self._rebuild_list()
        if hasattr(self, "platform_hint"):
            n = len(self._visible_items())
            self.platform_hint.text = (f"{self._platform} · {n} 条 · 点击卡片出操作"
                                       if self.items else "全部 · 左上角菜单加载列表")

    def _rebuild_list(self):
        self.list_box.clear_widgets()
        self.grid_layout.clear_widgets()
        for it in self._visible_items():
            starred = it.get("url", "") in self.stars
            self.list_box.add_widget(Row(it, self.play_item, self.download_item,
                                         self._toggle_star, starred))
            # 瀑布流卡片：整卡点击弹操作菜单（播/下载/录制/星标/外部）
            card = WaterfallCard(it, self._open_item_menu, starred=starred)
            self.grid_layout.add_widget(card)
        self._warm_covers()

    def _open_item_menu(self, item: dict):
        """点卡片弹操作菜单（仿小红书：卡片无按钮，点击出操作）。"""
        popup = Popup(title=item.get("name", "操作"), size_hint=(0.86, 0.5))
        box = BoxLayout(orientation="vertical", spacing=8, padding=10)

        def _do(fn, *a):
            def _cb():
                popup.dismiss()
                fn(*a)
            return _cb

        starred = item.get("url", "") in self.stars
        rows = [
            ("▶ 内置播放", _do(self.play_item, item)),
            ("⇱ 外部播放", _do(self._play_external, item.get("url", ""))),
            ("⬇ 下载", _do(self.download_item, item)),
            ("⏺ 录制（定时/分段用抽屉设置）", _do(self._record_item, item)),
            ("★ 取消星标" if starred else "☆ 加星标", _do(self._toggle_star, item)),
            ("取消", None),
        ]
        for text, cb in rows:
            if cb is None:
                b = Button(text=text, background_normal="",
                           background_color=(0.93, 0.93, 0.95, 1))
                b.bind(on_release=lambda *_: popup.dismiss())
            else:
                b = Button(text=text, background_normal="",
                           background_color=(0.97, 0.97, 0.98, 1))
                b.bind(on_release=lambda *_: cb())
            box.add_widget(b)
        popup.content = box
        popup.open()

    def _record_item(self, item: dict):
        """弹菜单里的录制：定时/分段取抽屉参数。"""
        try:
            secs = int(self.min_input.text.strip() or "0") * 60
        except ValueError:
            secs = 0
        try:
            seg = int(self.seg_input.text.strip() or "0")
        except ValueError:
            seg = 0
        self._start(item.get("url", ""), item.get("name", "录制"), "record", secs,
                    segment_minutes=seg)

    def _warm_covers(self):
        """后台把未缓存封面落盘，完成后刷新一次列表换成本地路径（省流量、离线可见）。

        每个封面 URL 每次会话只尝试一次（失败不重试），避免"下载失败→刷新→再下载"死循环。
        """
        covers_dir = os.path.join(save_dir(), "covers")
        todo = [c for c in {it.get("cover") for it in self.items}
                if c and c.startswith(("http://", "https://")) and c not in self._cover_attempted]
        if not todo:
            return
        for c in todo:
            self._cover_attempted.add(c)

        def work():
            for c in todo[:30]:
                try:
                    cached_cover_path(c, covers_dir)
                except Exception:  # noqa: BLE001
                    pass

            def cb(_dt):
                # 本地已有缓存的封面换文件路径展示；无新增待取时此轮回自然终止
                self._rebuild_list()
            Clock.schedule_once(cb)

        threading.Thread(target=work, daemon=True).start()

    def _toggle_star(self, item: dict):
        url = item.get("url", "")
        if not url:
            return
        if url in self.stars:
            self.stars.discard(url)
            self._log(f"已取消星标：{item.get('name', '')}")
        else:
            self.stars.add(url)
            self._log(f"已星标：{item.get('name', '')}")
        self._save_state()
        self._rebuild_list()

    def _toggle_auto_refresh(self, *_):
        if self.auto_refresh.active:
            try:
                secs = max(10, int(self.refresh_sec.text.strip() or "60"))
            except ValueError:
                secs = 60
            if self._refresh_event:
                Clock.unschedule(self._refresh_event)
            self._refresh_event = Clock.schedule_interval(lambda dt: self.refresh_list(), secs)
            self._log(f"定时刷新已开启，每 {secs} 秒")
        else:
            if self._refresh_event:
                Clock.unschedule(self._refresh_event)
                self._refresh_event = None
            self._log("定时刷新已关闭")

    # ---------- 播放 ----------
    def play_item(self, item: dict):
        self._play_url(item.get("url", ""))

    def _play_url(self, url: str):
        """默认内置播放；不可用自动回退外部播放器。"""
        if not url:
            self._log("无播放地址")
            return
        err = play_in_app(url)
        if err:
            self._log(err + "，尝试外部播放器")
            err2 = open_external(url)
            self._log("已请求外部播放器" if not err2 else err2)
        else:
            self._log("内置播放中（触摸显示暂停/进度条；点「关播放器」返回）")

    def _play_external(self, url: str):
        """强制调起外部播放器（VLC / 系统播放器等）。"""
        if not url:
            self._log("无播放地址")
            return
        err = open_external(url)
        self._log("已请求外部播放器" if not err else err)

    def _close_player(self, *_):
        stop_in_app_player()
        self._log("已关闭内置播放器")

    # ---------- 下载 / 录制 ----------
    def _read_settings(self) -> dict:
        """从输入框读取请求头 / 重试 / 分段 / 代理 / 限速 / 模板设置，并写回模块级自定义头。"""
        self.settings["ua"] = self.ua_input.text.strip()
        self.settings["referer"] = self.referer_input.text.strip()
        self.settings["cookie"] = self.cookie_input.text.strip()
        self.settings["max_retries"] = self.retry_input.text.strip() or "3"
        self.settings["segment_minutes"] = self.seg_input.text.strip() or "0"
        self.settings["proxy"] = self.proxy_input.text.strip()
        self.settings["speed_limit"] = self.speed_input.text.strip() or "0"
        self.settings["name_template"] = self.tpl_input.text.strip()
        self.settings["max_reconnect"] = self.reconnect_input.text.strip() or "5"
        CUSTOM_HEADERS["User-Agent"] = self.settings["ua"]
        CUSTOM_HEADERS["Referer"] = self.settings["referer"]
        CUSTOM_HEADERS["Cookie"] = self.settings["cookie"]
        import parser
        parser.CUSTOM_PROXY = self.settings["proxy"]
        self._save_state()
        return self.settings

    def _apply_preset(self, name: str):
        """一键填入站点预设 Referer / UA；UA 留空则保留当前输入。"""
        preset = HEADER_PRESETS.get(name, {})
        if preset.get("Referer"):
            self.referer_input.text = preset["Referer"]
        if preset.get("User-Agent"):
            self.ua_input.text = preset["User-Agent"]
        self._log(f"已套用 {name} 请求头预设")

    def _history_has(self, url: str) -> bool:
        return any(h.get("source_url") == url for h in self.history)

    def _add_history(self, name: str, source_url: str, save_path: str, mode: str):
        size = 0
        try:
            if os.path.exists(save_path):
                size = os.path.getsize(save_path)
        except OSError:
            size = 0
        record = {
            "name": name,
            "source_url": source_url,
            "save_path": save_path,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "size": size,
        }
        self.history.append(record)
        self._save_state()
        self._rebuild_history()
        self._rebuild_stats()

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self._state_dir()).free
        except OSError:
            return MIN_FREE_BYTES + 1

    def _rebuild_stats(self):
        total = len(self.history)
        total_size = sum(int(h.get("size", 0) or 0) for h in self.history)
        mb = total_size / (1024 * 1024)
        self.stats_label.text = f"统计：{total} 条，共 {mb:.1f} MB"

    @staticmethod
    def _fmt_size(n: int) -> str:
        n = int(n or 0)
        if n >= 1024 * 1024:
            return f"{n / 1024 / 1024:.1f}MB"
        if n >= 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n}B"

    # ---------- 收藏 / 列表 / 日志 导入导出 ----------
    def _export_favorites(self):
        favs = [it for it in self.items if it.get("url", "") in self.stars]
        if not favs:
            self._log("暂无星标条目可导出")
            return
        try:
            p = os.path.join(save_dir(), ".av_favorites.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(favs, f, ensure_ascii=False, indent=2)
            self._log(f"已导出 {len(favs)} 条收藏 → {os.path.basename(p)}")
        except OSError as e:
            self._log(f"导出收藏失败：{e}")

    def _import_favorites(self):
        try:
            p = os.path.join(save_dir(), ".av_favorites.json")
            if not os.path.exists(p):
                self._log("未找到收藏文件 .av_favorites.json")
                return
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                self._log("收藏文件格式不正确")
                return
            existing = {it.get("url", "") for it in self.items}
            added = 0
            for it in data:
                if not isinstance(it, dict) or not it.get("url"):
                    continue
                if it["url"] not in existing:
                    self.items.append(it)
                    existing.add(it["url"])
                    added += 1
                self.stars.add(it["url"])
            self._save_state()
            self._rebuild_list()
            self._log(f"已导入 {added} 条收藏")
        except (OSError, json.JSONDecodeError, TypeError) as e:
            self._log(f"导入收藏失败：{e}")

    def _export_m3u(self):
        items = self._visible_items()
        if not items:
            self._log("当前列表为空，无法导出 M3U")
            return
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            p = os.path.join(save_dir(), f"playlist_{ts}.m3u")
            lines = ["#EXTM3U"]
            for it in items:
                name = it.get("name", "未命名")
                url = it.get("url", "")
                if url:
                    lines.append(f"#EXTINF:-1,{name}")
                    lines.append(url)
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self._log(f"已导出 M3U（{len(items)} 条）→ {os.path.basename(p)}")
        except OSError as e:
            self._log(f"导出 M3U 失败：{e}")

    def _export_log(self):
        try:
            p = os.path.join(save_dir(), ".av_log.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(self.log_lines) if self.log_lines else (self.log.text or ""))
            self._log(f"日志已导出 → {os.path.basename(p)}")
        except OSError as e:
            self._log(f"导出日志失败：{e}")

    def _toggle_history(self):
        self._history_visible = not self._history_visible
        self._rebuild_history()

    def _rebuild_history(self):
        self.history_box.clear_widgets()
        if not self._history_visible:
            return
        if not self.history:
            self.history_box.add_widget(Label(text="暂无下载记录", size_hint_y=None,
                                              height=28, color=(0.7, 0.7, 0.7, 1)))
            return
        for h in reversed(self.history):
            line = f"· {h.get('name','')}  [{h.get('mode','')}]  " \
                   f"{self._fmt_size(h.get('size', 0))}  {h.get('time','')}"
            self.history_box.add_widget(Label(text=line, size_hint_y=None, height=24,
                                               halign="left", text_size=(None, None),
                                               color=(1, 1, 1, 1)))
            self.history_box.add_widget(Label(text=f"  URL: {h.get('source_url','')}",
                                              size_hint_y=None, height=20, font_size=10,
                                              halign="left", text_size=(None, None),
                                              color=(0.7, 0.7, 0.7, 1)))
            self.history_box.add_widget(Label(text=f"  文件: {h.get('save_path','')}",
                                              size_hint_y=None, height=20, font_size=10,
                                              halign="left", text_size=(None, None),
                                              color=(0.7, 0.7, 0.7, 1)))

    def _start(self, url: str, title: str, mode: str, max_seconds: int,
               kind_override=None, segment_minutes: int = 0):
        if not url:
            self._log("地址为空")
            return
        # 同地址仍在队列：跳过，避免重复下载/录制互踩文件
        if url in self._active_urls:
            self._log(f"该地址已在任务队列中，跳过：{title}")
            return
        # 磁盘空间检查：剩余不足阈值则跳过该任务
        if self._free_bytes() < MIN_FREE_BYTES:
            self._log("磁盘空间不足（<100MB），已跳过：" + title)
            return
        save = save_dir()
        settings = self._read_settings()
        try:
            max_retries = max(0, int(settings["max_retries"]))
        except ValueError:
            max_retries = 3
        try:
            speed_limit = max(0, int(settings["speed_limit"]))
        except ValueError:
            speed_limit = 0
        try:
            max_reconnect = max(0, int(settings["max_reconnect"]))
        except ValueError:
            max_reconnect = 5
        proxy = settings["proxy"] or None
        name_template = settings["name_template"]

        # 重复下载：按 source_url 命中仅日志提示，不强制阻止
        if self._history_has(url):
            self._log("提示：此前已下载过该地址")

        def on_progress(done, total, _speed):
            pct = f"{done}/{total}" if total else str(done)
            self._log(f"进度 {pct}  {title}")

        def on_status(s):
            self._log(f"{title}：{s}")

        def on_done(path):
            def cb(_dt):
                # 分段录制不做 ffmpeg 整段合并；其余录制成 ts 走转封装
                if mode == "record" and path.endswith(".ts") and segment_minutes <= 0:
                    self._convert_ts(path, title)
                else:
                    self._log(f"完成 → {os.path.basename(path)}")
                self._add_history(title, url, path, mode)
                self.tasks.pop(key, None)
                self._active_urls.discard(url)
                self._task_done(mode)
            Clock.schedule_once(cb)

        def on_fail(msg):
            def cb(_dt):
                self._active_urls.discard(url)
                self._task_done(mode)
                self._log(f"{title} 失败：{msg}")
            Clock.schedule_once(cb)

        task = build_task(url, save, title, mode, max_seconds=max_seconds,
                          kind_override=kind_override, max_retries=max_retries,
                          segment_minutes=segment_minutes, proxy=proxy,
                          speed_limit=speed_limit, name_template=name_template,
                          max_reconnect=max_reconnect,
                          on_progress=on_progress, on_status=on_status,
                          on_done=on_done, on_fail=on_fail)
        # 唯一 key：同名任务（如多个“自定义”）不再互相覆盖，停止按钮才能全部停掉
        key = f"{title}#{id(task)}"
        self.tasks[key] = task
        self._active_urls.add(url)
        if mode == "record":
            self._recording += 1
            self._wake_lock(True)
            self._log("已入队：" + title + "（录制中保持唤醒，灭屏不断流）")
        else:
            self._log(f"已入队：{title}")
        task.start()

    def _task_done(self, mode: str):
        """任务结束（成功/最终失败）后释放并发计数与 WakeLock。"""
        if mode == "record":
            self._recording = max(0, self._recording - 1)
            if self._recording == 0:
                self._wake_lock(False)

    def _wake_lock(self, acquire: bool):
        """录制期间持 PARTIAL_WAKE_LOCK，降低灭屏后任务被后台回收的概率。

        需要 buildozer.spec 里的 WAKE_LOCK 权限；非安卓环境（预览/桌面）静默跳过。
        """
        try:
            if acquire and self._wl is None:
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                Context = autoclass("android.content.Context")
                PowerManager = autoclass("android.os.PowerManager")
                pm = activity.getSystemService(Context.POWER_SERVICE)
                wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "VideoPlayerDL:record")
                wl.acquire()
                self._wl = wl
            elif not acquire and self._wl is not None:
                try:
                    self._wl.release()
                finally:
                    self._wl = None
        except Exception:  # noqa: BLE001
            self._wl = None

    def _find_ffmpeg(self) -> str | None:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            for n in ("ffmpeg", "ffmpeg.exe"):
                cand = os.path.join(d, n)
                if os.path.exists(cand):
                    return cand
        for n in ("ffmpeg", "ffmpeg.exe"):
            cand = os.path.join(save_dir(), n)
            if os.path.exists(cand):
                return cand
        return None

    def _convert_ts(self, ts_path: str, title: str):
        ff = self._find_ffmpeg()
        if not ff:
            self._log(f"录制完成（无 ffmpeg，保留 ts）→ {os.path.basename(ts_path)}")
            return
        mp4 = ts_path[:-3] + ".mp4"

        def work():
            try:
                subprocess.run([ff, "-y", "-i", ts_path, "-c", "copy", mp4],
                               check=True, capture_output=True, timeout=3600)
                self._log(f"转 MP4 完成 → {os.path.basename(mp4)}")
            except Exception as e:  # noqa: BLE001
                self._log(f"转 MP4 失败，保留 ts：{e}")

        threading.Thread(target=work, daemon=True).start()

    def download_item(self, item: dict):
        self._start(item.get("url", ""), item.get("name", "video"), "download", 0)

    # ---------- 自定义 ----------
    def custom_play(self):
        self._play_url(self.url_input.text.strip())

    def custom_download(self):
        url = self.url_input.text.strip()
        sel = self.type_spinner.text
        kind_override = "mp4" if sel == "MP4 点播" else ("hls" if sel == "HLS 直播" else None)
        self._start(url, "自定义", "download", 0, kind_override)

    def custom_record(self):
        url = self.url_input.text.strip()
        try:
            secs = int(self.min_input.text.strip() or "0") * 60
        except ValueError:
            secs = 0
        try:
            seg = int(self.seg_input.text.strip() or "0")
        except ValueError:
            seg = 0
        self._start(url, "自定义", "record", secs, segment_minutes=seg)

    # ---------- 监控：源 / 关注 ----------
    def _add_source(self):
        url = self.src_input.text.strip()
        if url and not any(u == url for u, _ in self.sources):
            self.sources.append((url, True))
            self.src_input.clear()
            self._rebuild_sources()
            self._save_state()

    def _clear_sources(self):
        self.sources.clear()
        self._rebuild_sources()
        self._save_state()

    def _rebuild_sources(self):
        self.src_box.clear_widgets()
        for url, _en in self.sources:
            self.src_box.add_widget(TagRow(url, lambda r, u=url: self._remove_source(u)))

    def _remove_source(self, url: str):
        self.sources = [(u, e) for u, e in self.sources if u != url]
        self._rebuild_sources()
        self._save_state()

    def _add_follow(self):
        word = self.fol_input.text.strip()
        if word and word not in self.follows:
            self.follows.append(word)
            self.fol_input.clear()
            self._rebuild_follows()
            self._save_state()

    def _clear_follows(self):
        self.follows.clear()
        self._rebuild_follows()
        self._save_state()

    def _rebuild_follows(self):
        self.fol_box.clear_widgets()
        for word in self.follows:
            self.fol_box.add_widget(TagRow(word, lambda r, w=word: self._remove_follow(w)))

    def _remove_follow(self, word: str):
        self.follows = [w for w in self.follows if w != word]
        self._rebuild_follows()
        self._save_state()

    def _keyword_hit(self, name: str) -> bool:
        if not self.follows:
            return False
        t = (name or "").lower()
        return any(f.lower() in t for f in self.follows)

    # ---------- 监控启停 ----------
    def _start_monitor(self):
        if self.poller and self.poller.is_alive():
            self._log("监控已在运行")
            return
        try:
            interval = int(self.interval_input.text.strip() or "30")
        except ValueError:
            interval = 30
        self.poller = MonitorPoller(on_items=self._on_items, on_log=self._log)
        self.poller.set_params(self.sources, interval)
        self.poller.start()
        self._log("监控已启动")

    def _stop_monitor(self):
        if self.poller:
            self.poller.stop()
            self.poller = None
        self._log("监控已停止")

    def _on_items(self, items: list[dict], source: str):
        def cb(_dt):
            for it in items:
                fp = f"{it.get('name','')}|{it.get('url','')}"
                if not fp or fp in self.seen:
                    continue
                self.seen.add(fp)
                if self._keyword_hit(it.get("name", "")):
                    if self.auto_dl.active:
                        self._start(it.get("url", ""), it.get("name", "命中"), "download", 0)
                        self._log(f"命中「{it.get('name')}」，已加入下载队列")
                    else:
                        self._log(f"命中「{it.get('name')}」（自动下载未开，仅记录）")
            self._save_state()
        Clock.schedule_once(cb)

    # ---------- 持久化 ----------
    def _state_dir(self) -> str:
        return save_dir()

    def _save_state(self):
        try:
            d = self._state_dir()
            with open(os.path.join(d, ".av_sources.json"), "w", encoding="utf-8") as f:
                json.dump(self.sources, f, ensure_ascii=False)
            with open(os.path.join(d, ".av_follows.json"), "w", encoding="utf-8") as f:
                json.dump(self.follows, f, ensure_ascii=False)
            with open(os.path.join(d, ".av_seen.json"), "w", encoding="utf-8") as f:
                json.dump(sorted(self.seen), f, ensure_ascii=False)
            with open(os.path.join(d, ".av_settings.json"), "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False)
            with open(os.path.join(d, ".download_history.json"), "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
            with open(os.path.join(d, ".av_stars.json"), "w", encoding="utf-8") as f:
                json.dump(sorted(self.stars), f, ensure_ascii=False)
        except OSError:
            pass

    def _load_state(self):
        try:
            d = self._state_dir()
            for fn, key in ((".av_sources.json", "sources"),
                            (".av_follows.json", "follows"),
                            (".av_seen.json", "seen"),
                            (".av_settings.json", "settings"),
                            (".download_history.json", "history"),
                            (".av_stars.json", "stars")):
                p = os.path.join(d, fn)
                if not os.path.exists(p):
                    continue
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if key == "sources":
                    self.sources = [(u, e) for u, e in data]
                elif key == "follows":
                    self.follows = list(data)
                elif key == "seen":
                    self.seen = set(data)
                elif key == "settings":
                    if isinstance(data, dict):
                        self.settings.update({k: str(v) for k, v in data.items()})
                elif key == "history":
                    if isinstance(data, list):
                        self.history = [h for h in data if isinstance(h, dict)]
                elif key == "stars":
                    if isinstance(data, list):
                        self.stars = {str(u) for u in data}
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        # 回填设置输入框与模块级自定义请求头 / 代理
        self.ua_input.text = self.settings.get("ua", "")
        self.referer_input.text = self.settings.get("referer", "")
        self.cookie_input.text = self.settings.get("cookie", "")
        self.retry_input.text = self.settings.get("max_retries", "3")
        self.seg_input.text = self.settings.get("segment_minutes", "0")
        self.proxy_input.text = self.settings.get("proxy", "")
        self.speed_input.text = self.settings.get("speed_limit", "0")
        self.tpl_input.text = self.settings.get("name_template", "")
        self.reconnect_input.text = self.settings.get("max_reconnect", "5")
        # 回填上次使用的列表地址（重启后自动带出，直接下拉或点刷新即可重拉）
        self.json_input.text = self.settings.get("json_url", "")
        if self.json_input.text:
            self.platform_hint.text = "上次地址已恢复 · 下拉或抽屉「刷新」重拉列表"
        CUSTOM_HEADERS["User-Agent"] = self.settings.get("ua", "")
        CUSTOM_HEADERS["Referer"] = self.settings.get("referer", "")
        CUSTOM_HEADERS["Cookie"] = self.settings.get("cookie", "")
        import parser
        parser.CUSTOM_PROXY = self.settings.get("proxy", "")
        self._rebuild_sources()
        self._rebuild_follows()
        self._rebuild_history()
        self._rebuild_list()
        self._rebuild_stats()


if __name__ == "__main__":
    MainApp().run()
