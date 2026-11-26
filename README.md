# VideoPlayerDL 安卓版（Kivy 工程）

桌面版（Windows / Linux）用的是 PyQt6，无法直接编译成 APK。本目录是按你的选择
用 **Kivy + Buildozer** 重写的安卓工程：下载 / 录制 / m3u8 解析 / AES 解密等纯逻辑
与桌面版一致，UI 层换成 Kivy，可构建成安卓安装包。

> 说明：构建环境需要 Android SDK/NDK，当前打包环境没有，因此这里交付的是
> **可构建的工程源码**，APK 文件需在本地或云端用 buildozer 构建（命令见下）。

## 文件

```
android_app/
├── main.py          # Kivy 界面 + 入口（含监控面板）
├── engine.py        # 纯线程下载/录制（MP4 / m3u8 / 直播录制）
├── monitor.py       # 多源轮询后台线程
├── parser.py        # JSON(ZWMAP/通用) + m3u8 解析 + AES 解密
├── requirements.txt
├── buildozer.spec   # buildozer 构建配置
├── .github/workflows/build-apk.yml   # GitHub Actions 云端构建
└── README.md
```

## 功能（安卓版）

- **全新卡片式界面（仿小红书/抖快风格）**：白底极简；顶部「汉堡菜单 ☰ | 平台 Tab
  （全部/斗鱼/抖音…按列表自动生成，点选即过滤） | 搜索🔍」；主区双列瀑布流封面卡片
  （底部信息条显示名称与★）；点整卡弹出操作菜单：内置播放 / 外部播放 / 下载 /
  录制 / 星标；底部状态条：统计 + 日志尾行 + 关播放器；
- **左侧抽屉菜单**：左上角 ☰ 打开，收纳全部配置——数据源（JSON URL/加载/定时刷新/
  视图切换）、监控与关注列表（加源/加关注/启停）、自定义直链与下载/录制参数
  （定时/分段/重试/重连/限速）、请求头（UA/Referer/Cookie）与代理、命名模板、
  站点预设（斗鱼/抖音/B站）、收藏与记录导入导出；
- 粘贴 JSON 列表 URL 加载（在抽屉「数据源」），支持「刷新」手动重载，勾选定时刷新
  并设间隔(秒)自动轮询；
- **下拉刷新**：主区（卡片/列表两种视图）顶部过度下拉后松手，自动重新拉取当前
  JSON 列表（2 秒防抖；未配地址时提示先去菜单填写）；
- **JSON 地址持久化**：点「加载」即记住列表地址（存 `.av_settings.json` 的
  `json_url`），关掉 APP 重开自动回填到抽屉输入框，下拉或点「刷新」即可重拉；
- 顶部平台 Tab 按已加载列表的分组自动生成（如「全部 斗鱼 抖音」），点选即过滤主区卡片；
- 顶部🔍展开搜索框，按名称/详情即时过滤，与平台过滤叠加；
- 直播录制：定时(分) 0=持续，录满自动停（参数在抽屉「下载/录制参数」）；
- **防盗链请求头**：在抽屉「下载/录制参数」里填写 User-Agent / Referer / Cookie；
  UA 留空回退默认 UA，Referer/Cookie 留空则不带；设置持久化到 `.av_settings.json`，
  下载与录制请求统一带上；
- **失败自动重试**：默认失败后重试 3 次，指数退避 `min(30, 2^n)` 秒后重建任务重跑，
  耗尽才上报失败；手动停止后不再重试；重试次数可在页面调整；
- **下载记录**：完成后写入 `.download_history.json`（名称 / 源 URL / 保存路径 / 时间 / 模式），
  页面「下载记录」入口可展开查看；同一源 URL 再次下载时日志提示「此前已下载」（不强制阻止）；
- **长直播分段录制**：可设分段分钟数（0=不切），到达时长关闭旧文件、开新文件
  `{标题}_{序号:03d}.ts`，on_done 回传首段路径；分段模式不做 ffmpeg 整段合并；
- **m3u8 多清晰度**：master 列表按带宽解析全部清晰度，下载默认取最高档；
  引擎层预留指定清晰度直链（variant_url）接口，UI 选档交互后续补充；
- **代理设置**：支持 HTTP / SOCKS5 代理（`http://host:port` 或 `socks5://host:port`），
  JSON 列表、m3u8、key、分片、录制请求统一走代理；设置持久化。SOCKS5 需
  `pip install requests[socks]`；
- **直播断流自动重连**：抓取 m3u8 / 分片出现网络异常时不直接判失败，按
  5s / 10s / 20s（上限 20s）指数退避重抓续录，已录分片保留、续接同一目录；
  默认重连 5 次，耗尽才上报失败；
- **列表搜索 / 过滤**：列表上方搜索框按名称 / 详情即时包含匹配，空串恢复全部；
- **收藏星标**：条目可 ★ 标星，星标条目置顶显示；星标持久化到 `.av_stars.json`；
- **单任务限速**：可设全局默认速率上限（字节/秒，0=不限），MP4 分块与 HLS 分片
  循环按目标速率休眠节流；
- **输出文件名模板**：支持占位符 `{name}` 主播名、`{title}` 标题、`{time}` 时间、
  `{quality}` 清晰度；模板为空沿用原命名，最终统一做非法字符清洗；
- **收藏集导入 / 导出**：星标条目可导出为 `.av_favorites.json`，亦可读回回填列表并
  重新标星；
- **站点请求头预设**：斗鱼 / 抖音 / B站一键填入常用 Referer（UA 留空沿用当前）；
- **磁盘空间检查**：入队前用 `shutil.disk_usage` 检查剩余空间，不足 100MB 日志提醒
  并跳过该任务；
- **导出 M3U 播放列表**：当前可见条目导出 `.m3u`（`#EXTM3U` 清单）供 VLC 等播放；
- **下载统计面板**：从下载记录聚合累计条数与总大小，页面顶部小面板展示；
- **日志导出**：「导出日志」按钮把当前日志文本保存到 `.av_log.txt`；
- **完整性校验**：MP4 下载后比对 Content-Length 与落盘大小；HLS 比对分片数；
  不一致判失败并复用重试包装重下；
- **多源监控 + 关注列表**：添加多条 JSON 源和关注关键词，定时轮询，名称命中任一
  关注词的新条目自动入队下载，按名称+URL 去重，源/关注/已处理记录持久化；
- **内置播放器（应用内播放）**：Android 系统 VideoView 叠加在界面上方直接播放，
  支持 MP4 与多数 HLS(m3u8) 直播，触摸屏幕显示系统控制条（暂停/进度/拖动）；
  列表/图文卡片点「播」即内置播放，内置不可用自动回退外部播放器；
  自定义直栏另有「外部」按钮强制调起 VLC / 系统播放器，操作行「关播放器」退出；
- **封面本地缓存**：封面后台落盘到下载目录 `covers/`（URL hash 命名），已缓存条目
  刷新/二次进入不再重新取图，离线也能看图；失败静默回退在线占位；
- **录制保活**：录制期间持 PARTIAL_WAKE_LOCK（需 WAKE_LOCK 权限，spec 已声明），
  降低灭屏后任务被系统回收的概率；全部录制结束后自动释放；
- **同地址去重**：同一条地址已在队列中时不再重复入队，避免同名文件互踩；
- 外部播放：通过系统 Intent 调起 VLC / 系统播放器打开 URL（自定义栏「外部」按钮）。

录完转 MP4：安卓系统默认不带 ffmpeg，录制产物保留 `.ts`；若设备上存在 ffmpeg
（PATH 或下载目录），会自动 `-c copy` 封装为 MP4，找不到则保留 ts。分段录制的多个
`.ts` 片段不做整段合并。

### 未移植（桌面版独有 / 当前版本未接入）

- **托盘完成通知**：QSystemTrayIcon 式桌面通知安卓不适用；完成通知依赖系统通知渠道，
  当前版本未接入；
- **剪贴板嗅探**：桌面版监听剪贴板自动识别链接，属桌面独有能力，安卓版未移植；
- **监控挂机守护**：后台进程自动重启守护属桌面 / 常驻服务能力，安卓后台定时与进程
  回收受限，当前版本未实现；
- **定时启停**：定时自动开始 / 停止下载或录制受安卓后台定时限制，当前版本未实现；
- **快捷键**：安卓触屏无物理键盘，桌面版快捷键未移植；
- **m3u8 清晰度选择弹窗**：引擎层已预留 variant_url 接口，UI 选档交互待补充，
  默认取最高档；
- **后台录制前台服务**：完全熄屏长时间录制建议后续接 ForegroundService + 通知栏，
  当前用 WakeLock 方案缓解；
- 未移植（桌面版独有）：mpv 内嵌（安卓以系统 VideoView 内置播放器替代）。

## 本地构建 APK（Linux 推荐）

前置：Python 3.10+、Java JDK 17、git；首次 buildozer 会自动下载 Android SDK/NDK。

```bash
# 1. 装 buildozer
pip install buildozer cython

# 2. 在 android_app 目录构建 debug APK
cd android_app
buildozer android debug

# 3. 产物
# bin/videoplayerdl-1.0.0-debug.apk
```

把 apk 传到安卓手机安装即可（需允许“未知来源安装”）。

## 云端构建（无需本地 Linux）

工程已自带 `.github/workflows/build-apk.yml`，自适应两种仓库布局（仓库根即本工程，
或仓库根含 `android_app/` 子目录）。把代码推到 GitHub，push 到 main 或在 Actions
页手动 Run，APK 在 Artifacts 下载。

## 桌面预览（可选）

```bash
pip install -r requirements.txt
python main.py
```

无安卓环境时播放按钮只会提示，下载 / 录制逻辑可正常在日志区查看。

## 存储与权限

- `buildozer.spec` 已申请 INTERNET、READ/WRITE_EXTERNAL_STORAGE；
- 下载/录制默认落盘到 `/storage/emulated/0/Movies`，Android 10+ 作用域存储下
  若写不进去，会回退到应用私有目录，可在日志看到提示；
- 加密 HLS 分片需要 cryptography（已在 requirements）；
- 持久化文件（均在下载目录下）：`.av_sources.json` / `.av_follows.json` /
  `.av_seen.json`（监控状态）、`.av_settings.json`（请求头 / 代理 / 重试 / 限速 /
  模板 / 分段 / 重连设置）、`.download_history.json`（下载记录，含文件大小）、
  `.av_stars.json`（星标）、`.av_favorites.json`（导出的收藏集）；
- SOCKS5 代理需额外安装 `requests[socks]`；统计面板目前展示条数与总大小，
  未记录单条时长。

## 修复记录（2026-11-26 代码审查）

审查修复了以下问题（逻辑层均已通过本地冒烟测试：ZWMAP/JSON 解析、m3u8
master/媒体列表解析、AES-128 解密 roundtrip、MP4 下载与 Range 断点续传、
失败自动重试链、监控线程启停）：

1. **`monitor.py` `_stop` 命名冲突（会崩溃）**：`self._stop = threading.Event()`
   覆盖了 `threading.Thread._stop()` 方法，任何 `join()` 都会抛
   `TypeError: 'Event' object is not callable`；改名 `_stop_evt`。
2. **`main.py` JSON 加载阻塞 UI 主线程（安卓 ANR 风险）**：`load_json_url`
   15 秒超时的同步网络请求直接在 UI 线程调用；改为后台线程 + `Clock` 回调。
3. **`engine.py` 重试导致文件名漂移（断点续传失效）**：重试重建任务时
   `{time}` 时间戳重新生成、文件名变化，`.part` 续传与覆盖旧半成品都失效；
   文件名改为在 `build_task` 外层计算一次并复用。
4. **`main.py` `open_external` 本地文件路径**：`from java.io import File`
   不是 pyjnius 语法必抛 ImportError；改为 `autoclass("java.io.File")`。
5. **`engine.py` 录制句柄泄漏**：录制线程两处提前 `return`（缺 cryptography、
   重连耗尽）不关闭输出文件；已补 `out.close()`。
6. **`engine.py` HLS 双请求**：指定 `variant_url` 时先抓 master 再抓 variant，
   浪费一次请求；已改为按需抓取。
7. **`main.py` 日志单行覆盖**：进度消息覆盖全部历史事件；改为追加式
   （保留最近 60 条），导出日志同步修复。
8. **`main.py` 无停止任务入口**：README 声称"手动停止后不再重试"但 UI 无
   停止按钮；已加「停止任务」按钮（级联停止重试包装与内层任务），任务
   key 改唯一命名避免同名互相覆盖。
9. **`requirements.txt` 首行笔误**：把文件名自身写成了依赖项，`pip install
   -r` 直接报错；已删除。
10. **`buildozer.spec` 无效键**：`android.need_arm` / `android.sdk` /
    `android.entrypoint` / `android.orientation` / `android.fullscreen`
    均非 buildozer 标准键会被忽略；已改为标准键（`orientation` /
    `fullscreen`）并显式指定 `android.archs = arm64-v8a, armeabi-v7a`。

## 二次更新（2026-11-26）：内置播放器 + 优化

1. **内置播放器**：新增 `play_in_app()` / `stop_in_app_player()`——Android 系统
   `VideoView` 叠加在 Kivy 界面上方播放（MediaPlayer 后端，支持 MP4 与多数
   HLS），挂载系统 `MediaController` 控制条；失败自动回退外部播放器；
   UI：自定义栏加「外部」按钮、操作行加「关播放器」按钮。
2. **封面本地缓存**：`parser.py` 新增 `cached_cover_path` / `local_cover_or_url` /
   `cover_cache_path`（纯逻辑层，无 Kivy 依赖）；UI 侧 `_warm_covers()` 后台
   预取落盘，完成刷新换本地路径；每个 URL 每会话只试一次，防死循环。
3. **录制保活**：录制入队时申请 `PARTIAL_WAKE_LOCK`（`buildozer.spec` 已加
   `WAKE_LOCK` 权限），灭屏不断流；全部录制结束 / 停止全部时释放。
4. **同地址去重**：`_active_urls` 在队地址拦截重复入队，任务完成/失败/停止时释放。
5. **on_fail 回调**：原 `_start` 的 `on_fail` 在后台线程直接改 UI，改为
   `Clock.schedule_once` 切主线程，顺手承担去重集合与 WakeLock 的释放收尾。

## 三次更新（2026-11-26）：卡片式主界面重构

按用户提供的参考图（小红书风格）整体重做界面：

1. **顶栏**：左上角 ☰ 汉堡菜单（开合左侧抽屉）＋ 中部平台 Tab（按列表分组自动
   生成：全部/斗鱼/抖音…，选中加粗带 ▾，点选即过滤卡片）＋ 右上角 🔍（展开
   搜索条）。
2. **主区瀑布流卡片**：双列卡片（大封面 + 底部信息条：名称/★），点击整卡弹
   操作菜单（Popup）：内置播放 / 外部播放 / 下载 / 录制 / 星标；卡片本身不再
   放小按钮。
3. **左侧抽屉菜单收纳全部配置**（原主界面十几行表单全部迁入，功能不变）：
   - 数据源：JSON URL / 加载 / 定时刷新 / 行式列表↔卡片瀑布切换；
   - 监控 · 关注：加源/加关注/启停/间隔/命中自动下载；
   - 下载/录制参数：自定义直链（播放/外部/下载/录制）、定时/分段/重试/重连/
     限速、UA/Referer/Cookie/代理/命名模板；
   - 预设/收藏/记录：站点预设、收藏导入导出、M3U/日志导出、下载记录展开。
4. **底部状态条**：统计 | 最新日志一行 | 关播放器 按钮。
5. 行式列表视图保留（抽屉里可切换），供喜欢紧凑文字列表的用户。
6. `Window.clearcolor` 加容错（无头/CI 环境不再崩溃）。

> 布局类：`WaterfallCard`（卡片）/ `DrawerCard`（抽屉分区）；
> 新方法：`_toggle_drawer` / `_toggle_search` / `_build_platform_tabs` /
> `_select_platform` / `_refresh_platforms` / `_open_item_menu` / `_record_item` /
> `_apply_filter`。已通过 AST 结构校验与编译检查；云电脑虚拟显卡无法跑
> Kivy 图形预览，UI 实际渲染请在 GitHub Actions 构建后真机验证。

## 四次更新（2026-11-26）：下拉刷新 + 地址记忆

1. **下拉刷新**：网格/列表两个 ScrollView 绑定 `on_scroll_stop`，顶部过度下拉
   （`scroll_y > 1.03`）后松手触发 `refresh_list()`，2 秒防抖；未配置地址时提示
   引导去抽屉填写；刷新中信息条显示「↓ 下拉刷新中…」。
2. **JSON 地址持久化（修复缺陷）**：此前主列表地址不落盘，重启即丢；现在
   `fetch_list` 时写入 `settings["json_url"]`（仅变化时落盘），`_load_state` 回填
   到抽屉输入框并提示「上次地址已恢复 · 下拉或抽屉「刷新」重拉列表」。
   注：监控源列表（`.av_sources.json`）此前就已持久化，不受影响。

> AI生成
