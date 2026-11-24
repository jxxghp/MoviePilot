# 星空站点页面适配

星空（`https://star-space.net/`）使用带 `p_` 前缀的 FireFly 定制页面。
用户资料需要包含 `StarSpace` 解析器的 MoviePilot 后端及索引资源 `3.0.33` 或更高版本。
仅更新站点资源无法给旧后端补充解析器；已有的搜索、音乐分区和下载规则保持原有协议。

## 配置与使用

- 站点地址填写 `https://star-space.net/`，认证继续使用站点 Cookie。
- 更新后端及站点资源后，重新刷新站点用户数据。资料入口为
  `p_index/index.php` → `p_user/user_detail.php?uid=…`，做种入口为
  `p_torrent/torrent_user.php?pop=8&uid=…`。
- RSS 请先登录站点，在 `p_rss/rss_create.php` 创建所需订阅。MoviePilot 从该页读取已创建的
  `p_rss/rss.php?key=…` 地址，不自动创建或删除站内 RSS。
- 如果已经保存了返回 404 的旧 RSS 地址，请在站点设置中替换为站内生成的新地址，或清空旧地址后重新获取。
  多个 RSS 有不同筛选条件时，直接填写需要的那一个。

「连接测试」通过只证明网页连通且 Cookie 有效，不能代替用户资料解析或用户认证接口的结果。
解析器只从已登录页面的本站个人资料链接确认用户编号，不使用 Cookie 中的 `ffid` 冒充资料解析成功。

## 验证范围

离线测试按现有公开星空适配定义构造合成页面，覆盖个人资料路由、上传/下载的省略 B 单位、
等级、加入时间、魔力、做种分页、Cookie 与分页隔离，以及已有 RSS 的读取与失败行为。
这些用例不是报告者账号的页面录制，也不代表已在其群晖 Docker 实例或真实星空账号中完成验收。

参考：[问题 #7494](https://github.com/jxxghp/MoviePilot/issues/7494)、
[公开的星空页面适配定义](https://github.com/pt-plugins/PT-depiler/blob/7817b40695537d0377b0640bbfe4bf0f991b98f8/src/packages/site/definitions/starspace.ts)。
[RSS 页面协议参考](https://github.com/linyuan0213/nexus-media/blob/98419056c91484cceb3bfc1df90eb7e9ebdcfdac/src/app/plugin_framework/builtin_plugins/autogenrss/backend/handlers/starspace.py)。
个人 Cookie、RSS key 和完整 RSS 地址均为凭据，请勿提交到公开 Issue。
