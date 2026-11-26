# VideoPlayerDL 优化说明

## 包含文件
- `.github/workflows/build-apk.yml`：补全系统构建依赖、Rust 工具链（供 `cryptography` 编译）、固定 `build-tools;33.0.2` 与 `platforms;android-33` 匹配 `buildozer.spec`，并增加 Buildozer/Gradle/Cargo 缓存。
- `android_app/buildozer.spec`：对齐 API 33 / NDK 25b 与常用依赖配置。
- `android_app/engine.py`：优化下载引擎（连接池复用 `create_session`、HLS 分片支持断点跳过已下载分片、直播录制复用 Session 与序列号去重、线程安全的重试状态管理）。
