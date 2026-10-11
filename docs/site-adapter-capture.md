# 站点适配采集

站点采集器已迁入独立仓库 [MoviePilot-Site-Collector](https://github.com/jxxghp/MoviePilot-Site-Collector)。普通用户无需安装 Python、Git 或 MoviePilot 后端：

1. 从 [采集器最新 Release](https://github.com/jxxghp/MoviePilot-Site-Collector/releases/latest) 下载对应系统的程序。
2. 按 [采集说明](https://github.com/jxxghp/MoviePilot-Site-Collector/blob/main/docs/site-adapter-capture.md) 在临时浏览器中登录、搜索并生成脱敏 ZIP。
3. 在本仓库创建“功能改进” Issue，选择“站点适配”，附加生成的 `moviepilot-site-capture-*.zip`。

Issue 及附件是公开内容。提交前请解压预览 `manifest.json`、`request.json`、`search.html`、`redaction-report.json`，确认没有账号或隐私信息。不要上传 Cookie、Authorization、通行密钥、原始 HTML、HAR 或浏览器网络归档。

源码运行、采集限制与脱敏协议均见 [独立仓库完整说明](https://github.com/jxxghp/MoviePilot-Site-Collector/blob/main/docs/site-adapter-capture.md)。本页面保留以兼容旧文档链接。
