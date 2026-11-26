# buildozer.spec —— VideoPlayerDL 安卓 APK 构建配置
# 用法：buildozer android debug（首次会自动下载 SDK/NDK）

[app]
title = VideoPlayerDL
package.name = videoplayerdl
package.domain = org.example

# 源码目录（本目录）
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,txt
source.exclude_dirs = tests, bin, .venv, __pycache__

version = 1.0.0

# Python 运行时与依赖
requirements = python3, kivy==2.3.0, requests, cryptography

# 目标 CPU 架构（现代安卓主流双架构；避免默认全架构拖慢构建）
android.archs = arm64-v8a, armeabi-v7a

# 应用权限：联网 + 读写存储（下载/录制落盘）+ WAKE_LOCK（录制期间灭屏不断流）
android.permissions = INTERNET, ACCESS_NETWORK_STATE, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, WAKE_LOCK
android.api = 33
android.minapi = 24
# 显式固定 build-tools 版本（与 android.api 33 配套；避免新 runner 默认拉 37
# 触发 "Aidl not found / license not accepted" 失败）
android.gradle.build_tools = 33.0.2
android.ndk = 25b

# 屏幕方向：竖屏（标准键名不带 android. 前缀）
orientation = portrait
fullscreen = 0

# 图标（可选，缺省用默认）
# icon.filename = %(source.dir)s/icon.png

# 入口约定：source.dir 下的 main.py（无 android.entrypoint 键）

[build]
# 并行编译线程数
parallel = 2

[app:android]
# 外部存储访问：Android 10+ 作用域存储下写公共 Movies 受限，
# 代码已内置回退应用私有目录（见 main.py save_dir）
