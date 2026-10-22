"""
从业务 OpenAPI 构建 moviepilot_api 的外部 MCP 输入合同。
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

OPERATION_DESCRIPTIONS = {
    "config.identifiers.get": "读取完整的自定义媒体识别词列表。",
    "config.identifiers.update": "替换完整的自定义媒体识别词列表。",
    "config.system.list": "列出已登记系统设置的精简机器可读契约。",
    "config.system.describe": "读取一个精确设置项的契约、当前值和修订号。",
    "config.system.get": "发现已登记系统设置，或读取一个精确设置项。",
    "config.system.update": "更新一个精确的已登记系统设置项。",
    "download.add": "将一个种子提交到 MoviePilot 标准下载流程。",
    "download.artist_collection": "将一个艺术家合集种子提交到音乐合集下载流程。",
    "download.clients": "列出已启用下载器的实例名称和提供方类型，不返回凭据。",
    "download.history.list": "按时间倒序分页读取下载历史。",
    "download.history.delete": "删除一条下载历史记录。",
    "download.paths": (
        "列出可传给 download.add 的下载器保存路径 URI；每项非空 name 也可作为 save_path。"
    ),
    "download.tasks.active": "列出正在下载的任务及其规范媒体上下文。",
    "filter.builtin": "列出内置种子过滤规则。",
    "filter.custom": "列出用户自定义的种子过滤规则。",
    "filter.custom.add": "创建一条自定义种子过滤规则。",
    "filter.custom.delete": "删除一条自定义种子过滤规则。",
    "filter.custom.update": "更新一条自定义种子过滤规则。",
    "filter.group.add": "创建一个命名过滤规则组。",
    "filter.group.delete": "删除一个命名过滤规则组。",
    "filter.group.update": "更新或重命名一个过滤规则组。",
    "filter.groups": "列出命名过滤规则组。",
    "library.exists": "按规范媒体身份查询已配置媒体服务器中的库存。",
    "library.latest": "为当前用户列出指定媒体服务器实例最近入库的项目。",
    "media.detail": "从选定元数据来源读取规范媒体详情。",
    "media.cache.get": "查看仅管理员可访问的 TMDB 识别缓存及统计数量。",
    "media.cache.delete": "按精确缓存键删除一条管理员可访问的 TMDB 识别缓存。",
    "media.cache.clear": "清空仅管理员可访问的 TMDB 识别缓存。",
    "media.episode_schedule": "读取 TMDB 中指定季的剧集上映信息。",
    "media.person.credits": "读取选定元数据来源中指定人物的作品。",
    "media.person.search": "在选定元数据来源中搜索人物和音乐艺术家。",
    "media.recognize": "根据标题、副标题或自定义规则上下文识别媒体身份。",
    "media.scrape": "为一个存储项目生成或刷新元数据。",
    "media.search": "在选定元数据来源中搜索规范媒体。",
    "music.album.get": "读取专辑详情、曲目、发行版本，以及按位置对应的艺术家名称和标识。",
    "music.album.related": "按来源原生专辑标识浏览相关专辑。",
    "music.artist.albums": "浏览艺术家的专辑、单曲、EP 或其他精确发行组类型。",
    "music.artist.get": "从选定音乐元数据来源读取艺术家的规范详情。",
    "music.artist.related": "按来源原生艺术家标识浏览相关艺术家。",
    "music.cache.clear": "清空仅管理员可访问的 MusicBrainz 识别缓存。",
    "music.cache.delete": "按精确键删除一条管理员可访问的 MusicBrainz 识别缓存。",
    "music.cache.get": "查看仅管理员可访问的 MusicBrainz 识别缓存及统计数量。",
    "music.explore": "浏览 MusicBrainz 榜单和新发行，或豆瓣音乐标签分类。",
    "music.recognize": "按精确音乐来源及其原生标识解析一首录音或一张专辑。",
    "plugin.capabilities": "查看已安装插件公开的运行时能力。",
    "plugin.config.get": "读取已加载插件的配置表单，以及合并已保存值后的默认配置。",
    "plugin.config.update": "替换已安装插件的完整配置并立即应用。",
    "plugin.data": "读取插件持久化数据的有界预览。",
    "plugin.install": "从获准来源安装或更新一个插件。",
    "plugin.installed": "列出已安装插件及其运行状态。",
    "plugin.market": "列出已配置市场中可用的插件。",
    "plugin.reload": "将一个已安装插件重新加载到当前进程。",
    "plugin.source.change": "把已安装插件切换到明确选定的在线来源修订。",
    "plugin.source.install": "从明确选定的在线来源安装一个尚未绑定来源的插件。",
    "plugin.source.options": "安装或切换来源前，查看候选来源与当前不可变来源身份。",
    "plugin.uninstall": "卸载一个插件并从已安装集合中移除。",
    "recommendation.list": "读取公开的媒体或音乐榜单与推荐。",
    "scheduler.list": "列出已登记调度任务及当前状态。",
    "scheduler.run": "立即执行一个已登记调度任务。",
    "search.results": "读取最近一次种子搜索的上下文和结果集。",
    "search.torrents": "按规范媒体身份搜索种子站点。",
    "site.cookie.update": "登录一个站点并刷新保存的认证 Cookie。",
    "site.cookie.set": "为一个站点保存浏览器取得的认证 Cookie 和可选 User-Agent，保留其他设置。",
    "site.list": "按状态和名称筛选已配置站点；认证字段仅向超级管理员返回。",
    "site.test": "测试一个已配置站点的连通性和认证状态。",
    "site.update": "更新一个已配置站点的完整设置。",
    "site.userdata": "读取最近采集的指定站点账号统计。",
    "slash.list": "列出智能体可分发的斜杠命令。",
    "slash.run": "通过 MoviePilot 消息入口执行一条完整斜杠命令。",
    "storage.list": "列出一个已配置存储位置的文件或目录。",
    "storage.settings": "读取已配置目录或存储设置。",
    "subscription.add": "创建一个电影、电视剧或音乐订阅。",
    "subscription.delete": "删除一个当前订阅。",
    "subscription.history": "列出已完成或已归档的订阅记录。",
    "subscription.list": "列出当前订阅。",
    "subscription.popular": "按条件和分页读取全站热门订阅。",
    "subscription.search": "立即搜索一个已有订阅。",
    "subscription.shares": "按条件和分页读取共享订阅。",
    "subscription.update": "更新一个已有电影、电视剧或音乐订阅。",
    "system.restart": "重启当前 MoviePilot 进程。",
    "system.update.check": "检查 MoviePilot v3 最高稳定版本和站点资源，发现新版本时刷新较旧的已准备目标。",
    "system.update.download": "后台下载并校验主程序或站点资源更新；target=all 表示一起准备所有最新更新。",
    "system.update.install": "安装已下载且通过校验的更新并重启；target=all 要求所有更新就绪后统一安装并只重启一次。",
    "system.update.status": "读取主程序和站点资源的检查、下载、校验或安装状态。",
    "system.upgrade.dev": "更新到当前 v3 开发分支并重启。",
    "system.versions": "列出 GitHub 中可用的 MoviePilot 发布版本。",
    "transfer.file": "执行手动文件转移与整理流程。",
    "transfer.history": "按条件和分页读取文件整理历史。",
    "transfer.history.delete": "删除一条整理历史，并可选择同时删除文件。",
    "workflow.list": "列出已配置工作流及执行状态。",
    "workflow.run": "从起点或恢复点执行一个已配置工作流。",
    "dashboard.cpu": "读取宿主机当前 CPU 使用百分比。",
    "dashboard.downloader": "读取下载器任务数、速度和可用空间汇总。",
    "dashboard.media.statistics": "读取电影、电视剧、剧集和音乐媒体库数量汇总。",
    "dashboard.memory": "读取 MoviePilot 进程和宿主机当前内存占用。",
    "dashboard.network": "读取宿主机当前网络收发计数。",
    "dashboard.processes": "列出当前运行时可见的宿主机进程。",
    "dashboard.storage": "读取本地文件系统容量和可用空间。",
    "dashboard.system": "读取宿主机、运行时、平台及运行时长摘要。",
    "dashboard.transfer.statistics": "按时间段读取文件整理数量汇总。",
    "database.backups.create": "创建、校验并原子发布一个托管数据库备份。",
    "database.backups.delete": "删除一个精确指定的托管数据库备份制品。",
    "database.backups.list": "列出托管数据库备份制品，不暴露宿主机路径。",
    "database.backups.verify": "校验一个精确指定的托管数据库备份制品的完整性。",
    "filter.test": "使用精确命名规则组测试标题及可选副标题。",
    "media.classification.fields": "读取媒体分类字段目录及来源能力。",
    "media.classification.policy.get": "读取当前生效的自动媒体分类策略。",
    "media.classification.policy.update": "校验并发布完整的自动媒体分类策略。",
    "media.classification.policy.validate": "校验尚未发布的自动媒体分类策略，不保存。",
    "media.classification.policy.preview": "为选定媒体或规范化事实预览自动分类结果。",
    "media.classification.policy.impact": "使用有界近期样本估算尚未发布的分类策略产生的变化。",
    "media.classification.policy.history": "读取已发布媒体分类策略的有界历史。",
    "media.classification.policy.rollback": "把选定历史媒体分类策略作为新修订发布。",
    "media.episode_group.seasons": "列出精确 TMDB 剧集组标识所定义的季。",
    "media.episode_groups": "列出指定电视剧在 TMDB 中可用的其他剧集组。",
    "media.recognize_file": "根据精确文件名和可选路径上下文识别规范媒体身份。",
    "media.seasons": "按精确媒体身份读取季列表，或回退到标题与年份查询。",
    "media.sources": "列出当前已登记的媒体元数据来源。",
    "plugin.clone": "为已安装插件创建可配置分身；suffix 留空时自动分配下一个空闲实例标识，复用已停用分身的后缀会连同保留配置重新启用该分身。",
    "plugin.clone.restorable": "列出指定插件已停用且仍保留配置的分身，可通过同后缀创建操作恢复。",
    "plugin.history": "读取指定插件的市场更新说明与历史。",
    "plugin.market.sync_wiki": "从 MoviePilot Wiki 刷新已配置插件市场仓库。",
    "plugin.rating": "读取指定插件的当前评分汇总。",
    "plugin.rating.submit": "提交或替换当前用户对指定插件的评分。",
    "plugin.ratings": "读取指定插件集合的评分汇总。",
    "plugin.releases": "列出指定插件来源的可用发布版本。",
    "plugin.reset": "删除插件已保存的配置与数据，并恢复默认运行状态。",
    "plugin.runtime.status": "读取插件运行时的收敛、加载和失败状态。",
    "plugin.statistics": "读取公开插件安装统计。",
    "scheduler.progress": "读取精确指定的调度任务的当前进度。",
    "search.recommend": "使用已配置推荐模型对种子搜索结果排序或推荐。",
    "search.title": "直接按自由文本标题和可选媒体条件搜索种子站点。",
    "site.add": "使用完整认证与搜索设置创建一个站点。",
    "site.auth.options": "列出站点账号认证提供方及所需输入定义。",
    "site.authenticate": "认证受支持站点的账号，并保存取得的站点授权状态。",
    "site.category": "列出指定站点支持的种子分类。",
    "site.cookiecloud.sync": "启动已配置站点的 CookieCloud 同步。",
    "site.delete": "按持久化站点标识删除一个站点。",
    "site.mapping": "读取已配置站点域名到名称的映射。",
    "site.priorities.update": "替换指定站点标识对应的优先级。",
    "site.reset": "删除全部已配置站点，并重新启动 CookieCloud 同步。",
    "site.resource": "按分类和关键词浏览指定站点的种子资源。",
    "site.rss": "列出已选入 RSS 订阅处理的站点。",
    "site.searchable": "列出已启用且支持精确媒体类型的站点。",
    "site.statistic": "读取精确站点域名对应的账号和流量统计。",
    "site.statistics": "读取所有已配置站点的最新账号和流量统计。",
    "site.supporting": "列出已安装资源支持的站点索引器定义。",
    "site.userdata.latest": "读取每个已配置站点最近采集的账号统计。",
    "site.userdata.refresh": "刷新并返回指定站点的账号统计。",
    "storage.delete": "从已配置存储提供方删除一个精确文件或目录。",
    "storage.manage": "对精确存储目标执行提供方定义的管理操作。",
    "storage.mkdir": "在精确存储目录项目下创建一个命名子目录。",
    "storage.rename": "重命名精确存储项目，可选择按媒体规则递归重命名。",
    "subscription.delete_by_media": "删除当前用户可访问且匹配规范媒体身份的订阅。",
    "subscription.files": "读取可访问订阅的本地媒体库及整理文件覆盖情况。",
    "subscription.execution.list": "列出当前用户可见的近期持久化订阅搜索批次。",
    "subscription.execution.get": "读取当前用户可见的一个持久化订阅搜索批次。",
    "subscription.execution.cancel": "请求取消一个尚未越过下载副作用边界的持久化订阅搜索批次。",
    "subscription.find": "按规范媒体身份和可选季查找一个可访问订阅。",
    "subscription.follow.add": "按精确分享用户标识关注一个订阅分享用户。",
    "subscription.follow.delete": "按精确分享用户标识取消关注一个订阅分享用户。",
    "subscription.follow.list": "列出当前用户关注的订阅分享用户标识。",
    "subscription.fork": "根据共享订阅定义创建一个本地订阅。",
    "subscription.get": "按持久化订阅标识读取一个可访问订阅。",
    "subscription.history.delete": "删除一条可访问的订阅历史记录。",
    "subscription.metadata.refresh": "启动全系统订阅 TMDB 元数据刷新。",
    "subscription.refresh": "启动已配置的全系统订阅刷新任务。",
    "subscription.reset": "重置一个可访问订阅，使其可以再次处理。",
    "subscription.search_all": "立即搜索当前用户可访问的全部订阅。",
    "subscription.share": "把一个可访问订阅发布到 MoviePilot 分享服务。",
    "subscription.share.delete": "按分享标识删除一条共享订阅发布记录。",
    "subscription.share.statistics": "读取订阅分享者的贡献与复用次数汇总。",
    "subscription.status.update": "把一个可访问订阅设为运行、暂停或停止。",
    "subscription.user.list": "列出指定可访问 MoviePilot 用户名下的公开订阅。",
    "subtitle.search.media": "按规范媒体身份和可选季、集搜索字幕提供方。",
    "subtitle.search.title": "按自由文本标题和可选媒体条件搜索字幕提供方。",
    "system.module.list": "列出已启用模块标识及本地化名称。",
    "system.module.catalog": "列出宿主模块与服务类型选项，供界面选择器使用。",
    "system.module.settings": "列出管理员可启用或停用的内置模块。",
    "system.module.test": "对指定已启用模块执行内置可用性测试。",
    "system.network.targets": "列出获准网络测试目标及脱敏目标地址，不暴露请求路径、查询参数或凭据。",
    "system.network.test": "测试一个获准目标，或受约束的历史 URL 输入的连通性。",
    "torrent.cache.clear": "删除全部种子上下文缓存。",
    "torrent.cache.delete": "按站点域名和缓存哈希删除一个种子上下文缓存。",
    "torrent.cache.get": "查看种子上下文缓存及已识别的媒体身份。",
    "torrent.cache.refresh": "从已配置 RSS 或爬虫来源刷新种子缓存。",
    "torrent.cache.reidentify": "替换或重新计算一个种子缓存上下文的媒体身份。",
    "transfer.episode_format.recommend": "根据文件样本推荐剧集编号提取模板。",
    "transfer.history.clear": "删除旧整理历史，保留文件和持久化失败任务记录。",
    "transfer.history.redo": "对一条整理历史启动 AI 辅助重新整理。",
    "transfer.history.redo_batch": "对明确列出的整理历史记录启动 AI 辅助批量重新整理。",
    "transfer.history.replan": "为没有文件副作用的识别失败历史提交已核实的电影或电视剧身份和季集修正，保留原任务及失败证据。",
    "transfer.manual_history": "检查给定存储项目是否已有成功整理历史。",
    "transfer.manual_review": "读取一个等待人工复核的持久化整理任务。",
    "transfer.manual_review.resolve": "记录一个持久化整理人工复核操作的已授权决定。",
    "transfer.manual_reviews": "分页读取等待人工复核或重试恢复的持久化整理任务。",
    "transfer.name": "为源路径和媒体身份预览整理后的目标名称。",
    "transfer.queue": "列出文件整理队列中的待处理项目。",
    "transfer.queue.delete": "从整理队列移除一个精确存储项目，并停止其整理。",
    "transfer.target_path": "根据给定源存储项目解析已配置的整理目标。",
    "workflow.actions": "列出内置工作流动作定义及参数契约。",
    "workflow.create": "根据完整定义创建一个工作流。",
    "workflow.delete": "按持久化工作流标识删除一个已配置工作流。",
    "workflow.event_types": "列出可以触发工作流的事件类型。",
    "workflow.fork": "根据共享工作流定义创建一个本地工作流。",
    "workflow.get": "读取一个已配置工作流的完整定义。",
    "workflow.pause": "停用一个已配置工作流的自动执行。",
    "workflow.plugin.actions": "列出已安装插件提供的工作流动作，可按插件标识筛选。",
    "workflow.reset": "重置一个已配置工作流的定义和执行状态。",
    "workflow.share": "把一个已配置工作流发布到 MoviePilot 分享服务。",
    "workflow.share.delete": "按分享标识删除一条共享工作流发布记录。",
    "workflow.shares": "按名称和分页条件读取共享工作流。",
    "workflow.start": "启用一个已配置工作流的自动执行。",
    "workflow.update": "替换一个已配置工作流的定义。",
    "config.user.get": "读取当前用户功能开关、运行时能力和实际权限。",
    "config.public.get": "按精确键读取一个明确公开的系统设置项。",
    "system.usage.statistics": "读取当前用户可访问的安装版本和运行使用报告。",
    "plugin.folders.get": "读取管理员完整插件文件夹分组配置。",
    "plugin.folders.update": "替换管理员完整插件文件夹分组配置。",
    "plugin.folder.create": "创建一个命名插件文件夹。",
    "plugin.folder.update": "增量重命名插件文件夹或更新其展示设置。",
    "plugin.folder.delete": "删除一个命名插件文件夹，保留其中已安装插件。",
    "plugin.folder.plugins.update": "替换指定插件文件夹中的有序插件标识列表。",
    "plugin.folder.plugin.assign": "把已安装插件移入指定文件夹，并移除其其他文件夹归属。",
    "plugin.folder.plugin.remove": "从指定文件夹移除一个已安装插件，不卸载插件。",
    "plugin.loglevel.get": "列出指定插件的本体与分身，以及各实例配置和实际生效的日志等级。",
    "plugin.loglevel.set": "设置插件实例日志等级覆盖，立即生效且不再跟随全局等级。",
    "plugin.loglevel.clear": "清除插件实例日志等级覆盖，立即恢复跟随全局等级。",
    "plugin.default_target.set": "将一个插件实例设为默认调用目标，自动清除此前默认目标。",
    "plugin.default_target.clear": "仅当指定实例为当前默认目标时，清除其默认调用目标标记。",
    "plugin.instance.set_enabled": "启用或停用插件本体或分身；停用只停止运行，保留配置以便以后重新启用。",
    "plugin.instance.purge": "永久删除明确选中的实例数据：保存配置、插件数据行、独立数据库和数据目录；分身还会删除实例记录。未明确选择的项目不删除。",
}


FIELD_DESCRIPTIONS = {
    "include_value": "是否在完整设置契约之外返回当前值。",
    "offset": "结果偏移量，从 0 开始。",
    "rule_ids": "要返回的精确规则标识；在查询串中重复传入 rule_ids，省略时列出该操作对应的全部规则。",
    "group_names": "要返回的精确规则组名称；在查询串中重复传入 group_names，省略时列出全部规则组。",
    "action_name": "需要返回能力契约的精确动作名称。",
    "allow_unrecognized": "无法解析规范媒体身份时，是否仍允许下载。",
    "album": "录音或订阅关联的专辑标题。",
    "album_id": "音乐搜索、发现或艺术家专辑列表返回的来源原生专辑标识。",
    "album_type": "音乐专辑或发行组类型；音乐发现可用 album、single、ep、broadcast、other、compilation、soundtrack、live、remix 筛选。",
    "apikey": "支持 API 密钥认证的站点所使用的密钥。",
    "area": "种子搜索流程使用的可选地区条件。",
    "artist_id": "音乐搜索或专辑详情返回的来源原生艺术家标识。",
    "artist_name": "艺术家音乐合集保留的身份名称。",
    "audio_format": "请求或记录的音频容器、编码格式，例如 FLAC、MP3。",
    "audio_lossless": "音频结果是否为无损。",
    "audio_quality": "订阅音质规则，例如 hires、lossless、lossy。",
    "backdrop": "媒体或订阅保存的背景图地址。",
    "background": "是否异步执行整理，并在完成前返回。",
    "batch_id": "稳定的订阅搜索批次标识。",
    "basename": "不含父目录路径的文件名。",
    "best_version": "为 1 时启用普通洗版升级。",
    "best_version_full": "为 1 时启用完整洗版升级。",
    "bit_depth": "已记录音频位深，单位为位。",
    "bitrate": "已记录音频码率，单位为比特每秒。",
    "body": (
        "选定操作的请求值；必须匹配对应 oneOf 分支，标量与对象请求体不能互换。"
    ),
    "category": "媒体分类或过滤规则组分类，具体含义由操作决定。",
    "cache_key": "对应识别缓存查询操作返回的精确缓存键。",
    "channel": "最初提交下载的消息渠道。",
    "children": "当前存储项目下的子项目。",
    "code": "二次验证代码或站点专用认证秘密。",
    "command": "包含前导斜杠及全部参数的完整命令。",
    "completed_episode": "订阅已完成的最高集号。",
    "completed_tracks": "订阅目前已接收的不同专辑曲目数量。",
    "current_site_id": "当前处理订阅执行的已配置站点标识。",
    "cookie": "站点认证 Cookie，属于秘密信息。",
    "configured_level": "插件实例自行配置的日志等级覆盖；未设置或已过期时为 null。",
    "count": "当前页最多返回的记录数。",
    "current_audio_format": "当前持有最佳版本的音频格式。",
    "current_bit_depth": "当前持有最佳版本的音频位深。",
    "current_bitrate": "当前持有最佳版本的音频码率。",
    "current_priority": "当前持有最佳版本的计算优先级。",
    "current_sample_rate": "当前持有最佳版本的音频采样率。",
    "custom_words": "本次媒体处理使用的自定义识别或重命名词。",
    "classification_policy_revision": "生成已保存分类快照的策略修订号。",
    "classification_rule_id": "选中已保存分类的稳定规则标识。",
    "classification_source": "已保存分类快照记录的选择来源。",
    "date": "历史记录的创建或完成时间戳。",
    "date_elapsed": "种子发布时间距今的可读时长。",
    "days": "推荐时间窗口，单位为天。",
    "deletedest": "删除整理历史时是否同时删除整理后的目标文件。",
    "deletesrc": "删除整理历史时是否同时删除记录中的源文件。",
    "description": "媒体、种子或订阅的可读说明。",
    "dest": "整理历史中记录的目标路径。",
    "dest_fileitem": "整理记录保存的序列化目标存储项目。",
    "dest_storage": "包含整理目标的已配置存储名称。",
    "directory_type": "需要返回的目录配置子类型。",
    "domain": "用于匹配和请求的站点主机名或域名。",
    "download_hash": "记录关联的下载器原生种子哈希。",
    "downloader": "已配置下载器的实例名称。",
    "downloadvolumefactor": "站点报告的种子下载流量倍率。",
    "douban_sort": "豆瓣音乐分类排序：U 为综合，S 为评分，R 为最新，O 为最热。",
    "drive_id": "存储提供方的原生驱动器标识。",
    "effect": "订阅使用的视频或版本特效筛选表达式。",
    "effective_level": "结合实例覆盖、过期时间和全局等级计算的实际日志等级。",
    "error": "工作流、提供方或执行过程的可读错误说明。",
    "entity": "音乐发现实体：recording 表示曲目，album 表示发行组。",
    "enclosure": "索引器结果提供的种子下载地址或附件地址。",
    "episode_detail": "手动整理使用的剧集映射详情。",
    "episode_format": "手动整理使用的集号格式规则。",
    "episode_group": "用于其他剧集排序方式的 TMDB 剧集组标识。",
    "episode_offset": "加到识别集号上的整数偏移量。",
    "episode_part": "一集拆分为多个文件时的分段编号。",
    "episode_priority": "逐集保存的最佳版本优先级状态。",
    "episodes": "历史记录中的集号表达式，例如 E01-E03。",
    "errmsg": "整理失败时记录的错误说明。",
    "expected_revision": "前次读取取得的当前修订号，用于拒绝并发状态变化。",
    "expires_at": "插件日志等级覆盖的失效时间；null 表示永不过期，无时区时间按 UTC 解读。",
    "exclude": "用于排除匹配版本的正则或过滤表达式。",
    "extension": "文件扩展名，是否包含前导点以存储返回值为准。",
    "fileid": "存储提供方的原生项目标识。",
    "fileitem": "storage.list 返回的一个完整源存储项目。",
    "fileitems": "同一次手动整理包含的其他源存储项目。",
    "files": "历史记录保存的序列化文件列表。",
    "filter": "站点或订阅使用的命名过滤规则或规则表达式。",
    "filter_groups": "订阅按顺序应用的过滤规则组名称。",
    "force": "为 true 时强制刷新市场或安装插件。",
    "freedate": "站点报告的种子免费结束时间戳。",
    "freedate_diff": "种子免费期剩余秒数。",
    "fresh_sort": "推荐来源使用的新鲜度排序方式。",
    "from_begin": "是否从工作流首个动作重新执行，而不恢复已有进度。",
    "from_history": "是否把整理输入视为来自已有历史记录。",
    "future": "来源支持时是否包含未来推荐时段。",
    "genre_id": "筛选共享或热门订阅的题材标识。",
    "grabs": "站点报告的种子已完成下载次数。",
    "group": "动态发现设置项时使用的已登记设置分组。",
    "hit_and_run": "种子是否受下载后保种要求约束。",
    "id": "给定记录的持久化数据库标识。",
    "identifiers": "完整且有序的自定义识别词规则列表。",
    "image": "历史记录保存的图片地址。",
    "include": "发布版本必须匹配的正则或过滤表达式。",
    "include_group_refs": "是否包含仅被规则组引用的自定义规则。",
    "include_usage": "是否返回引用各规则组的订阅或默认设置。",
    "include_values": "是否返回完整设置值，而非发现摘要。",
    "instance_id": "plugin.loglevel.get 返回的精确插件实例标识。",
    "config": "清理范围开关：删除该插件实例已保存的配置值。",
    "plugin_data": "清理范围开关：删除共享插件数据表中该实例的数据行。",
    "own_database": "清理范围开关：销毁该插件实例的独立数据库。",
    "data_directory": "清理范围开关：删除该实例整个数据目录；总是先销毁其独立数据库。",
    "is_default_target": "该实例是否为插件默认调用目标，供未指定实例的调用使用。",
    "enabled": "目标是否启用；分类对象停用后不参与求值，插件实例停用后停止运行但保留配置和展示信息。",
    "is_active": "已配置站点是否启用。",
    "jobid": "scheduler.list 返回的精确调度任务标识。",
    "key": "用于缩小预览范围的可选精确插件数据键。",
    "keyword": "发现设置或筛选存储项目时使用的不区分大小写子串。",
    "level": "目标日志等级，可选 DEBUG、INFO、WARNING、ERROR、CRITICAL。",
    "labels": "附加到媒体、分类结果或种子结果的标签。",
    "lack_episode": "订阅仍缺少的集数。",
    "last_update": "订阅最近更新时间戳。",
    "library_category_folder": "是否在目标媒体库创建或使用分类层目录。",
    "library_type_folder": "是否在目标媒体库创建或使用媒体类型层目录。",
    "limit_count": "流控统计窗口内允许的最大站点请求数。",
    "limit_interval": "站点流控统计窗口长度，单位为秒。",
    "limit_seconds": "相邻站点请求之间的最小间隔，单位为秒。",
    "limit": "最多返回的近期订阅搜索批次数。",
    "logid": "手动整理使用的一个下载历史或整理日志标识。",
    "logids": "手动整理包含的多个下载历史或整理日志标识。",
    "match_field": "列表更新或移除时，用于匹配目标项的对象字段。",
    "match_value": "更新列表项时与 match_field 比较的精确值。",
    "max_chars": "最多返回的序列化插件数据字符数。",
    "max_results": "插件目录结果数上限，范围 1 至 200；省略时返回完整目录。",
    "max_rating": "筛选共享或热门订阅的最高评分。",
    "media_category": "分配给媒体的 MoviePilot 媒体库分类。",
    "media_category_id": "稳定的媒体分类标识，需与当前分类路径快照分别保存。",
    "media_id": "来源原生媒体标识，始终与搜索返回的精确 media_source 配对。",
    "media_source": "元数据来源标识，保留与 media_id 一同返回的精确值。",
    "media_type": "筛选推荐或规则组使用的媒体类型。",
    "min_bit_depth": "最低可接受音频位深，单位为位。",
    "min_bitrate": "最低可接受音频码率，单位为比特每秒。",
    "min_filesize": "手动整理接受的最小源文件字节数。",
    "min_listen_count": "音乐推荐要求的最低收听次数。",
    "min_rating": "筛选共享或热门订阅的最低评分。",
    "min_sample_rate": "最低可接受音频采样率，单位为赫兹。",
    "min_sub": "筛选热门订阅的最低订阅人数。",
    "mode": "操作模式；music.explore 接受 chart 或 fresh，整理历史记录 move、copy、link、softlink 或 reflink。",
    "modify_time": "存储项目的修改时间戳。",
    "mtype": "操作要求的媒体类型或订阅历史分类。",
    "music_cue_enable": "订阅级可选 CUE 整理设置，省略时继承系统默认值。",
    "music_type": "音乐身份层级：按操作支持范围选择 recording、album 或 artist。",
    "include_candidates": "是否包含待人工复核的未确认音乐资源和相关专辑，默认 false；候选没有目标媒体身份，不能自动下载。",
    "name": "站点、存储项目、订阅或规则组的可读名称。",
    "new_name": "现有过滤规则组的新名称。",
    "new_rule_id": "现有自定义过滤规则的新稳定标识。",
    "next_run_at": "下一次计划订阅搜索时间；没有后续执行计划时为 null。",
    "note": "记录附带的结构化辅助元数据。",
    "operation": "设置更新方式：replace、merge_dict、upsert_list_item 或 remove_list_item。",
    "operation_id": "白名单中的精确操作标识，用于选择对应 oneOf 分支。",
    "page": "从 1 开始的结果页码。",
    "page_url": "种子结果的公开详情页地址。",
    "parent_fileid": "存储提供方的原生父目录标识。",
    "password": "站点登录密码，属于秘密信息。",
    "past": "来源支持时是否包含过去推荐时段。",
    "path": "当前记录表示的存储路径或历史路径。",
    "path_params": (
        "仅替换选定操作固定路由占位符的资源身份；名称和类型必须匹配对应 oneOf 分支。"
    ),
    "peers": "站点报告的种子正在下载人数。",
    "person_id": "人物搜索返回的来源原生人物标识。",
    "pickcode": "当前项目关联的 115 存储提取码。",
    "plugin_id": "精确的已安装插件或市场插件标识。",
    "poster": "媒体或订阅保存的海报地址。",
    "preview": "仅校验并预览手动整理结果，不提交文件变更。",
    "pri": "站点搜索优先级，升降序含义沿用站点 API 约定。",
    "pri_order": "分配给种子结果的索引器优先顺序。",
    "proxy": "站点是否使用已配置的代理。",
    "public": "站点是否作为公开索引器处理。",
    "pubdate": "种子发布时间戳。",
    "publish_time": "自定义过滤规则中的发布时间筛选表达式。",
    "quality": "视频或发布版本的质量筛选表达式。",
    "query": (
        "通过查询字符串发送的筛选和控制值；名称、类型、默认值和枚举必须匹配所选操作的 oneOf 分支。"
    ),
    "range_name": "命名的推荐时间范围。",
    "release_version": "需要指定版本时使用的精确插件发布版本。",
    "remove_keys": "merge_dict 合并给定值后需要移除的对象键。",
    "render": "站点请求是否需要浏览器渲染。",
    "reorganize": "是否允许手动整理再次处理已经整理过的项目。",
    "repo_url": "用于解析安装来源的获准插件仓库地址。",
    "resolution": "视频分辨率筛选表达式，例如 1080p、2160p。",
    "rss": "站点 RSS 订阅地址。",
    "rule_id": "自定义过滤规则的稳定标识。",
    "rule_string": "规则组保存的有序过滤表达式。",
    "sample_rate": "已记录音频采样率，单位为赫兹。",
    "save_path": (
        "下载或订阅使用的下载器端配置保存路径，或已配置下载目录的精确名称别名。"
    ),
    "scrape": "手动整理后是否生成元数据和图片。",
    "search_interval": "计划搜索间隔，单位为整数小时，范围 1 至 8760；null 使用系统间隔。",
    "last_search": "最近一次订阅搜索尝试的 UTC 时间戳，只读。",
    "search_imdbid": "为 1 时在订阅搜索中使用 IMDb 身份。",
    "season": "媒体、搜索、订阅或整理操作使用的季号。",
    "seasons": "历史记录中的季号表达式。",
    "seeders": "过滤规则的最低做种人数表达式，或种子实际做种人数。",
    "server": "媒体服务器实例列表返回的精确实例名称。",
    "setting_key": "config.system.get 发现结果返回的精确已登记设置键。",
    "show_secrets": "是否返回未脱敏的秘密值，仅可在明确授权后使用。",
    "site": "种子结果关联的来源站点标识。",
    "site_cookie": "种子结果附带的站点 Cookie，属于秘密信息。",
    "site_downloader": "来源站点选定的下载器实例。",
    "site_id": "site.list 返回的持久化站点标识。",
    "site_name": "来源站点的可读名称。",
    "site_order": "来源站点配置的搜索顺序。",
    "site_proxy": "种子来源站点是否使用已配置代理。",
    "site_ua": "来源站点关联的 User-Agent。",
    "sites": "搜索或订阅范围包含的精确站点标识。",
    "size": "文件或种子大小，单位为字节。",
    "size_range": "自定义过滤规则接受的种子大小范围表达式。",
    "sort": "存储列表的排序字段或排序表达式。",
    "sort_by": "推荐结果使用的排序字段。",
    "sort_type": "共享或热门订阅使用的升序或降序方式。",
    "source": "操作选定的精确元数据或推荐来源。",
    "src": "整理历史记录的源路径。",
    "src_fileitem": "整理记录保存的序列化源存储项目。",
    "src_storage": "包含整理源项目的已配置存储名称。",
    "start_episode": "订阅请求的起始集号。",
    "state": "站点、订阅、市场或整理的当前状态筛选条件。",
    "status": "用于筛选历史或描述记录的整理成功状态。",
    "storage": "操作使用的已配置存储名称或存储类型。",
    "storage_type": "需要返回的存储提供方类型。",
    "stype": "接口请求的热门订阅分类。",
    "subscribe_id": "subscription.list 返回的持久化订阅标识。",
    "subtitle": "媒体识别时与标题配合使用的可选副标题。",
    "target_path": "手动整理使用的目标路径。",
    "target_storage": "接收手动整理结果的已配置存储名称。",
    "tags": "逗号分隔的豆瓣音乐分类标签，仅用于豆瓣音乐发现来源。",
    "template": "传 default 读取内置媒体分类策略模板；省略时读取当前生效策略。",
    "thumbnail": "存储提供方返回的缩略图地址。",
    "timeout": "单次站点请求的超时秒数。",
    "title": "操作使用的媒体、种子、订阅或历史标题。",
    "tmdbid": "媒体搜索或详情返回的 TMDB 媒体标识。",
    "token": "站点认证令牌，属于秘密信息。",
    "torrent_description": "下载历史记录的种子发布说明。",
    "torrent_in": "search.results 或 search.torrents 返回的完整候选种子。",
    "torrent_name": "下载历史记录的种子发布名称。",
    "torrent_site": "下载历史记录的来源站点名称。",
    "total_episode": "订阅预期总集数。",
    "total_tracks": "音乐项目预期或已记录的曲目数。",
    "transfer_task_id": "历史记录关联的稳定持久化整理任务标识。",
    "transfer_type": "手动整理方式，例如 move、copy、link、softlink、reflink。",
    "trigger_type": "工作流触发方式筛选：timer、event、manual 或 all。",
    "type": "所选操作要求的媒体或存储项目类型。",
    "type_name": "仅凭来源标识无法区分时使用的显式媒体类型名。",
    "ua": "站点认证请求使用的 User-Agent 字符串。",
    "uploadvolumefactor": "站点报告的种子上传流量倍率。",
    "url": "当前字段表示的站点、存储或种子地址。",
    "userid": "下载历史记录的消息渠道用户标识。",
    "username": "所选操作要求的 MoviePilot 或站点用户名。",
    "value": "选定更新方式使用的完整替换值、对象片段或单个列表项。",
    "volume_factor": "种子展示的上传与下载流量倍率组合标签。",
    "vote": "订阅保存的媒体平均评分。",
    "with_cover": "是否要求推荐结果包含封面。",
    "workdate": "读取站点历史用户统计时使用的日期。",
    "workflow_id": "workflow.list 返回的持久化工作流标识。",
    "year": "用于区分同名媒体的发行或首映年份。",
}

FIELD_DESCRIPTIONS.update(
    {
        "all": "必须全部匹配的子条件。",
        "action": "所选操作要求的精确提供方动作或工作流动作标识。",
        "actions": "工作流或流程按顺序执行的动作定义。",
        "add_time": "工作流定义的创建时间戳。",
        "animated": "工作流连接在编辑器中是否显示动画。",
        "any": "至少需要匹配一个的子条件。",
        "attempt": "当前工作流节点的执行尝试次数。",
        "attempts": "按工作流节点或操作身份索引的尝试计数。",
        "auto_paused": "整理失败是否自动暂停了关联订阅。",
        "backoff": "工作流动作每次失败后应用的重试退避倍率。",
        "branch_policy": "控制后续路径选择的工作流分支策略。",
        "cat": "site.category 返回的精确站点分类标识。",
        "check_only": "仅校验或预览推荐，不实际过滤搜索结果。",
        "can_cancel": "当前订阅执行是否允许取消。",
        "categories": "分类策略中的完整有序媒体分类定义。",
        "category_id": "分类规则或选择结果指向的稳定媒体分类标识。",
        "companies": "媒体关联的制作公司或工作室。",
        "concurrency_key": "用于串行执行相同运行时键动作的工作流表达式。",
        "content_rating": "媒体的内容分级。",
        "countries": "分类规则使用的规范化国家或地区代码。",
        "condition": "运行时求值的工作流分支或流程条件表达式。",
        "context": "供后续动作使用的持久化工作流执行上下文。",
        "current_action": "当前选中或正在执行的工作流动作标识。",
        "cleanup_error": "清理整理源或目标时记录的错误。",
        "cleanup_status": "操作完成后整理文件清理的当前状态。",
        "data": "序列化工作流动作配置或运行时载荷。",
        "decision": "从接口声明枚举中选择的人工复核决定。",
        "episode": "缩小字幕或媒体搜索范围的集号。",
        "enrichment_mode": "填充分类事实时使用的元数据补全模式。",
        "errors": "按动作身份索引或排序的工作流执行错误。",
        "enabled": "目标是否启用；分类对象停用后不参与求值，插件实例停用后停止运行但保留配置和展示信息。",
        "entity_type": "分类规则使用的音乐实体类型。",
        "example_limit": "最多返回的代表性影响样例数。",
        "execution_status": "订阅快照返回的当前执行状态。",
        "event_conditions": "额外的工作流事件筛选条件。",
        "event_type": "workflow.event_types 返回的精确事件类型。",
        "execution_config": "工作流运行限制、并发设置和失败策略配置。",
        "execution_state": "可用于恢复的持久化工作流执行状态。",
        "fail_policy": "控制停止、继续或分支行为的工作流失败策略。",
        "failure_stage": "整理或工作流操作失败所在的阶段。",
        "fallbacks": "没有分类规则匹配时使用的兜底分类或标签动作。",
        "facts": "分类预览时求值的规范化媒体事实。",
        "field": "条件引用的规范化分类事实字段。",
        "field_aliases": "把来源专用字段映射到规范化分类字段的可选别名。",
        "field_sources": "规范化分类事实的来源溯源信息。",
        "filetype": "预览整理目标名称使用的媒体文件类型。",
        "filtered_indices": "推荐模型选中的搜索结果下标，从 0 开始。",
        "finished_actions": "持久化执行状态中已完成的工作流动作标识。",
        "finished_at": "工作流节点或执行结束的时间戳。",
        "flows": "连接工作流动作节点的连接定义。",
        "folder_name": "plugin.folders.get 返回的精确插件文件夹名称。",
        "extensions": "扩展提供的额外规范化分类事实。",
        "genre_keys": "分类规则使用的规范化 MoviePilot 题材键。",
        "genre_names": "来源提供的题材名称，作为分类事实使用。",
        "genres": "分类规则使用的规范化音乐题材值。",
        "genre_ids": "自动分类规则接受的题材标识。",
        "history_id": "历史查询操作返回的持久化整理或订阅历史标识。",
        "history_ids": "一次批量重新整理请求明确包含的持久化整理历史标识。",
        "icon": "工作流、网络目标、插件或分类使用的图标名称或地址。",
        "identity": "分类事实使用的稳定来源原生媒体身份。",
        "inputs": "当前工作流动作消费的命名输入绑定。",
        "interval": "下一次工作流动作尝试前的重试等待秒数。",
        "input": "分类预览使用的媒体或规范化事实输入。",
        "album_type": "音乐专辑或发行组类型；音乐发现可用 album、single、ep、broadcast、other、compilation、soundtrack、live、remix 筛选。",
        "artist_country": "音乐艺术家关联的国家或地区。",
        "artists": "被分类实体关联的音乐艺术家名称。",
        "adult": "媒体是否标记为成人内容。",
        "job_id": "scheduler.list 返回的精确调度任务标识。",
        "join_policy": "控制下游何时继续执行的工作流汇合策略。",
        "language": "分类规则使用的规范化媒体语言代码。",
        "last_time": "工作流最近一次执行的时间戳。",
        "kind": "请求选定的分类规则类型或预览输入类型。",
        "max_attempts": "工作流重试策略允许的最大尝试次数。",
        "max_workers": "执行配置允许的最大并行动作数。",
        "message": "工作流运行时或提供方返回的可读结果说明。",
        "media": "分类预览使用的媒体元数据输入。",
        "media_types": "分类规则适用的媒体类型。",
        "moduleid": "system.module.list 返回的精确已加载模块标识。",
        "movie": "按顺序求值的自动电影分类规则。",
        "music": "分类规则使用的音乐专用规范化事实。",
        "musicbrainz_release_id": "选定专辑发行组下的精确 MusicBrainz Release UUID，可选；不得作为 media_id 使用。",
        "music_release_regions": "音乐整理使用的可选 ISO 3166-1 发行地区优先顺序。",
        "music_release_scripts": "音乐整理使用的可选 ISO 15924 文字字形优先顺序。",
        "networks": "媒体关联的电视台或流媒体平台。",
        "node_states": "按工作流节点身份索引的持久化运行状态。",
        "nodes": "按动作身份索引的持久化工作流节点运行状态。",
        "not": "必须不匹配的子条件组。",
        "operator": "求值一个分类条件所用的比较操作符。",
        "origin_country": "自动分类规则匹配的制作国家代码。",
        "original_language": "自动分类规则匹配的原始语言代码。",
        "outputs": "当前工作流动作生成的命名输出映射。",
        "page_size": "每页最多返回的记录数。",
        "phase": "订阅执行的当前阶段。",
        "params": "提供方为选定认证或存储动作定义的 JSON 参数。",
        "policy": "完整的自动媒体分类策略或策略草稿。",
        "plugin_ids": "需要返回评分汇总的精确插件标识。",
        "position": "工作流编辑器中一个动作节点的坐标。",
        "provider_id": "提供分类事实的来源原生标识。",
        "provider_name": "提供分类事实的来源可读名称。",
        "production_countries": "自动分类规则匹配的制作国家代码。",
        "priority": "服务端保存的分类规则排序优先级。",
        "progress": "当前工作流执行的数值或结构化进度。",
        "rating": "接口声明范围内的插件数值评分。",
        "reason": "人工复核决定附带的可读理由。",
        "recursive": "为 true 时按媒体规则递归重命名子文件。",
        "recovery_action": "失败或部分完成的整理所报告的恢复动作。",
        "release_year": "自动分类规则匹配的发行年份。",
        "result": "持久化工作流动作结果值。",
        "revision": "已发布分类策略修订号或预期修订号。",
        "result_payload": "人工复核记录的结构化外部操作结果。",
        "release_status": "分类规则使用的音乐发行状态。",
        "rules": "从高到低按优先级求值的有序分类规则。",
        "restore_previous": "创建分身时是否复用同后缀已停用分身的保留配置；false 表示从源插件模板重建该分身配置。",
        "retry": "当前动作的工作流重试策略定义。",
        "retry_count": "操作已经消耗的重试次数。",
        "retry_exhausted": "操作是否已耗尽配置的重试次数。",
        "rulegroup_name": "filter.groups 返回的精确过滤规则组名称。",
        "run_count": "工作流已执行次数。",
        "running_tasks": "当前正在执行的工作流任务标识。",
        "runtime": "用于安全恢复的持久化工作流运行时元数据。",
        "secondary_types": "分类规则使用的音乐发行组次级类型。",
        "sample_limit": "影响分析最多检查的近期记录或样本数。",
        "samples": "分类影响分析明确提供的规范化事实样本。",
        "schema_version": "服务端要求的分类策略结构版本。",
        "sources": "分类规则适用的元数据来源标识；空集合表示全部来源。",
        "share_comment": "随共享项目发布的可选说明。",
        "share_id": "分享列表操作返回的持久化 MoviePilot Server 分享标识。",
        "share_title": "发布订阅或工作流时使用的公开标题。",
        "share_uid": "需要关注或取消关注的精确 MoviePilot Server 分享用户标识。",
        "share_user": "发布订阅或工作流时使用的公开贡献者名称。",
        "skip_success": "是否跳过已记录为整理成功的文件。",
        "site_url": "选取站点统计使用的已配置站点地址或主机名。",
        "updated_at": "持久化对象或执行状态最近更新的时间戳。",
        "started_at": "工作流节点或执行开始的时间戳。",
        "subid": "需要改变状态或处理进度的持久化订阅标识。",
        "suffix": "自动分类规则匹配的文件后缀或扩展名。",
        "target": "分类规则输出，包含分类标识及可选标签。",
        "target_id": "system.network.targets 返回的获准内置网络测试目标标识。",
        "task_id": "transfer.manual_reviews 返回的稳定持久化整理任务标识。",
        "timer": "工作流定时执行使用的计时设置或 cron 表达式。",
        "torrent_hash": "torrent.cache.get 返回的精确站点域名条目的缓存哈希。",
        "tv": "按顺序求值的自动电视剧分类规则。",
        "version": "操作选定的插件发布版本或结构版本。",
        "wiki_url": "插件市场同步使用的获准 MoviePilot Wiki 来源地址。",
        "when": "应用分类规则目标前求值的条件组。",
        "x": "工作流编辑器中的水平坐标。",
        "y": "工作流编辑器中的垂直坐标。",
    }
)


MODEL_DESCRIPTIONS = {
    "AgentCommandRunRequest": "斜杠命令执行请求。",
    "Body_add_api_v1_download_add_post": "下载提交请求。",
    "Body_download_artist_collection_api_v1_download_artist_collection_post": (
        "艺术家音乐合集下载提交请求。"
    ),
    "CustomFilterRuleCreateRequest": "自定义过滤规则创建请求。",
    "CustomFilterRuleUpdateRequest": "自定义过滤规则更新请求。",
    "CustomIdentifiersUpdateRequest": "完整替换自定义识别词的请求。",
    "DownloadHistory-Input": "一条下载历史记录。",
    "FileItem-Input": "已配置存储提供方返回的一个文件或目录。",
    "FilterRuleGroupCreateRequest": "过滤规则组创建请求。",
    "FilterRuleGroupUpdateRequest": "过滤规则组更新请求。",
    "JsonData-Input": "兼容 JSON 的任意辅助数据。",
    "ManualTransferItem": "手动文件转移与整理请求。",
    "MediaSource": "与来源原生媒体标识配对的规范元数据来源标识。",
    "MediaType": "MoviePilot 媒体类型。",
    "MusicRecognizeRequest": "待解析为规范音乐元数据的精确来源原生录音或专辑身份。",
    "PluginInstanceEnabledRequest": "插件实例启停请求；停用后保留配置，供以后重新启用。",
    "PluginInstanceLogLevelUpdateRequest": "插件实例日志等级覆盖更新请求。",
    "PluginInstancePurgeRequest": "插件实例清理范围，每项仅在明确选中时删除。",
    "PluginSourceChangeRequest": "明确的在线来源切换请求，受当前身份修订号保护。",
    "PluginSourceInstallRequest": "为尚未绑定来源的插件明确选择在线来源的安装请求。",
    "Site-Input": "完整站点配置及运行状态。",
    "SiteCookieUpdate": "用于刷新已保存 Cookie 和 User-Agent 的站点登录请求。",
    "SiteCookieSet": "需要持久化的浏览器认证 Cookie 和可选 User-Agent，保留其他站点设置。",
    "Subscribe": "电影、电视剧或音乐订阅输入模型。",
    "SystemSettingsUpdateRequest": "一个已登记系统设置项的更新请求。",
    "SystemUpdateRequest": "选定更新目标：application、resources 或 all；all 同时准备和安装，并只重启一次。",
    "SubscriptionExecutionStatus": "订阅刷新执行状态和进度摘要。",
    "TorrentInfo": "搜索返回的一个候选种子。",
    "TransferHistory-Input": "一条文件整理历史记录。",
}

MODEL_DESCRIPTIONS.update(
    {
        "ClassificationCategory": "包含媒体库路径和默认标签的稳定媒体分类定义。",
        "ClassificationCondition": "自动媒体分类规则中的一个叶子条件。",
        "ClassificationConditionGroup-Input": "递归的 all、any 或 not 条件组。",
        "ClassificationFacts": "自动分类策略求值的规范化媒体事实。",
        "ClassificationFactsPreviewInput": "直接提供给分类预览的规范化事实。",
        "ClassificationFactSource": "由其他来源补全的分类事实的提供方身份。",
        "ClassificationIdentityFacts": "分类求值使用的稳定来源原生身份。",
        "ClassificationImpactRequest": "比较未发布分类策略与当前生效策略的请求。",
        "ClassificationMediaFacts": "分类规则使用的规范化电影、电视剧或共用媒体事实。",
        "ClassificationMediaPreviewInput": "提供给分类预览的选定媒体搜索结果。",
        "ClassificationMusicFacts": "自动分类规则使用的音乐专用事实。",
        "ClassificationPolicy-Input": "完整且带版本的自动媒体分类策略。",
        "ClassificationPolicyPublishRequest": "使用比较并交换修订号校验并发布完整分类策略的请求。",
        "ClassificationPolicyRollbackRequest": "将历史分类策略作为新修订发布的请求。",
        "ClassificationPolicyValidateRequest": "仅校验完整分类策略、不保存的请求。",
        "ClassificationPreviewRequest": "针对媒体或规范化事实预览分类策略的请求。",
        "ClassificationRule-Input": "一条有序自动媒体分类规则。",
        "ClassificationTarget": "包含可选分类标识和附加标签的规则输出。",
        "PluginCloneRequest": "插件分身标识及可选展示名称请求。",
        "PluginMarketSyncRequest": "使用获准 Wiki 来源同步插件市场的请求。",
        "PluginRatingRequest": "当前用户提交的插件数值评分。",
        "PluginFoldersData": "插件文件夹名称到有序插件标识或展示配置的完整映射。",
        "PluginFolderConfigData": "一个插件文件夹的有序成员和可选展示设置。",
        "PluginFolderUpdateRequest": "增量修改插件文件夹名称或展示设置的请求。",
        "PluginFolderPluginsUpdateRequest": "有条件地替换一个插件文件夹有序成员的请求。",
        "Body_recommend_search_results_api_v1_search_recommend_post": "传给已配置模型的种子搜索结果及推荐控制参数。",
        "SiteAuth": "受支持的站点账号认证提供方及精确参数值。",
        "SitePriorityUpdate": "一个已配置站点标识及待替换的搜索优先级。",
        "ManageRequest": "已配置存储目标、提供方定义的动作及动作参数。",
        "SubscribeShare": "共享订阅定义或发布元数据。",
        "EpisodeFormatRecommendItem": "用于推断集号提取模板的文件样本。",
        "BatchTransferHistoryRedoRequest": "AI 辅助批量重新整理中明确指定的历史记录标识。",
        "TransferHistoryReplanRequest": "对零文件副作用的识别拒绝提交已核实的媒体身份及季集修正。",
        "TransferManualReviewRequest": "持久化整理操作的已授权决定和可选结果。",
        "Workflow-Input": "创建或更新操作接受的完整工作流定义。",
        "Action-Input": "工作流中的一个可执行动作节点。",
        "ActionPosition": "工作流编辑器中的动作节点坐标。",
        "ActionRetry": "工作流动作的重试限制与时间设置。",
        "ActionFlow-Input": "工作流动作节点间的一条有向连接。",
        "WorkflowExecutionConfig": "工作流并发、汇合、分支和失败策略。",
        "WorkflowExecutionState-Input": "可恢复的持久化工作流执行状态。",
        "WorkflowNodeState": "一个工作流动作节点的持久化运行状态。",
        "WorkflowRuntimeState": "完整的持久化工作流运行和进度状态。",
        "WorkflowShare": "共享工作流定义或发布元数据。",
    }
)


def _apply_field_descriptions(schema: Mapping[str, Any]) -> dict[str, Any]:
    """
    补充中文字段说明，并保留端点已经提供的中文契约。
    """
    rewritten: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            rewritten[key] = _apply_field_descriptions(value)
        elif isinstance(value, list):
            rewritten[key] = [
                _apply_field_descriptions(item) if isinstance(item, Mapping) else deepcopy(item) for item in value
            ]
        else:
            rewritten[key] = deepcopy(value)
    properties = rewritten.get("properties")
    if isinstance(properties, dict):
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            existing = field_schema.get("description")
            if isinstance(existing, str) and existing.strip() and re.search(r"[\u3400-\u9fff]", existing):
                continue
            description = FIELD_DESCRIPTIONS.get(str(field_name))
            if not description:
                raise ValueError(f"MCP field guidance is missing: {field_name}")
            field_schema["description"] = description
    return rewritten


def _rewrite_schema_refs(
    schema: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any]:
    """
    把 OpenAPI components 引用改写为独立 MCP schema 的 $defs 引用。
    """
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        name = reference.rsplit("/", 1)[-1]
        if name not in definitions:
            definitions[name] = {}
            source = components.get(name)
            if not isinstance(source, Mapping):
                raise ValueError(f"OpenAPI 缺少请求模型: {name}")
            definitions[name] = _rewrite_schema_refs(
                source,
                components=components,
                definitions=definitions,
            )
        return {"$ref": f"#/$defs/{name}"}

    rewritten: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            rewritten[key] = _rewrite_schema_refs(
                value,
                components=components,
                definitions=definitions,
            )
        elif isinstance(value, list):
            rewritten[key] = [
                _rewrite_schema_refs(
                    item,
                    components=components,
                    definitions=definitions,
                )
                if isinstance(item, Mapping)
                else deepcopy(item)
                for item in value
            ]
        else:
            rewritten[key] = deepcopy(value)
    return rewritten


def _parameter_object_schema(
    parameters: Sequence[Mapping[str, Any]],
    *,
    location: str,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any] | None:
    """
    把 OpenAPI path/query 参数投影为网关结构化对象。
    """
    selected = [parameter for parameter in parameters if parameter.get("in") == location]
    if not selected:
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in selected:
        name = str(parameter["name"])
        raw_schema = parameter.get("schema")
        if not isinstance(raw_schema, Mapping):
            raw_schema = {}
        field_schema = _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        )
        if parameter.get("description") and "description" not in field_schema:
            field_schema["description"] = parameter["description"]
        properties[name] = field_schema
        if parameter.get("required"):
            required.append(name)
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _request_body_schema(
    operation: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    definitions: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """
    读取一个 OpenAPI operation 的 JSON 请求体及必填性。
    """
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None, False
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None, bool(request_body.get("required"))
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        media = next((item for item in content.values() if isinstance(item, Mapping)), None)
    raw_schema = media.get("schema") if isinstance(media, Mapping) else None
    if not isinstance(raw_schema, Mapping):
        return None, bool(request_body.get("required"))
    return (
        _rewrite_schema_refs(
            raw_schema,
            components=components,
            definitions=definitions,
        ),
        bool(request_body.get("required")),
    )


def _person_credits_operation(openapi: Mapping[str, Any]) -> dict[str, Any]:
    """
    合并四个来源端点为稳定 media.person.credits 网关合同。
    """
    paths = openapi.get("paths", {})
    source_paths = {
        "douban": "/api/v1/douban/person/credits/{person_id}",
        "tmdb": "/api/v1/tmdb/person/credits/{person_id}",
        "bangumi": "/api/v1/bangumi/person/credits/{person_id}",
        "anilist": "/api/v1/anilist/person/credits/{person_id}",
    }
    for source_path in source_paths.values():
        if source_path not in paths:
            raise ValueError(f"OpenAPI 缺少人物作品端点: {source_path}")
    representative = paths[source_paths["douban"]].get("get")
    responses = deepcopy(representative.get("responses")) if isinstance(representative, Mapping) else {}
    return {
        "summary": "查询人物参演作品",
        "parameters": [
            {
                "name": "source",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": list(source_paths)},
                "description": "人物标识所属的元数据来源。",
            },
            {
                "name": "person_id",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"},
                "description": "元数据来源分配的人物标识。",
            },
            {
                "name": "page",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "default": 1},
            },
            {
                "name": "count",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "description": "Bangumi 和 AniList 使用的每页条数；其他来源忽略此值。",
            },
        ],
        "responses": responses,
    }


def _resolve_openapi_operation(
    operation_id: str,
    route: Any,
    openapi: Mapping[str, Any],
) -> Mapping[str, Any]:
    """
    按固定路由读取 OpenAPI operation，并处理多来源合成路由。
    """
    if operation_id == "media.person.credits":
        return _person_credits_operation(openapi)
    path_item = openapi.get("paths", {}).get(route.path)
    if not isinstance(path_item, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 路径: {operation_id} -> {route.path}")
    operation = path_item.get(str(route.method).lower())
    if not isinstance(operation, Mapping):
        raise ValueError(f"OpenAPI 缺少 operation 方法: {operation_id} -> {route.method} {route.path}")
    return operation


def _apply_operation_overrides(
    operation_id: str,
    query_schema: dict[str, Any] | None,
    body_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool | None]:
    """
    补充同一路由多 operation 时无法由 FastAPI 自动表达的语义约束。
    """
    if operation_id == "media.person.search" and query_schema is not None:
        query_schema["properties"]["type"] = {
            "type": "string",
            "const": "person",
            "description": "固定为 person，表示搜索人物。",
        }
        required = query_schema.setdefault("required", [])
        if "type" not in required:
            required.append("type")
    if operation_id in {"plugin.installed", "plugin.market"} and query_schema is not None:
        state = "installed" if operation_id == "plugin.installed" else "market"
        query_schema["properties"]["query"]["description"] = (
            "可选关键词，匹配插件标识、名称、说明和作者，不区分大小写。"
        )
        query_schema["properties"]["state"] = {
            "type": "string",
            "const": state,
            "description": f"固定为 {state}，仅选择该状态的插件目录条目。",
        }
        required = query_schema.setdefault("required", [])
        if "state" not in required:
            required.append("state")
    if operation_id == "music.explore" and query_schema is not None:
        descriptions = {
            "media_source": (
                "音乐探索来源：musicbrainz 支持 chart 和 fresh 模式；doubanmusic 按标签浏览。"
            ),
            "mode": "MusicBrainz 模式：chart 查询收听榜单，fresh 查询新发行专辑。",
            "entity": "榜单对象：recording 表示曲目，album 表示发行组；fresh 模式返回专辑。",
            "range_name": "ListenBrainz 榜单时间范围：this_week、this_month、this_year、week、month 或 year。",
            "sort_by": "ListenBrainz 榜单排序：listen_count.desc 按收听次数降序，listen_count.asc 按升序。",
            "sort": "当前 ListenBrainz 实现接受的新发行专辑排序方式。",
            "days": "新发行专辑的回溯或前瞻天数，最小为 1，最大值以端点限制为准。",
            "past": "fresh 模式是否包含今天之前发行的专辑。",
            "future": "fresh 模式是否包含今天之后发行的专辑。",
            "min_listen_count": "chart 模式的最低 ListenBrainz 收听次数。",
            "with_cover": "为 True 时仅保留有封面图片的结果。",
            "tags": "逗号分隔的豆瓣音乐标签，仅在 media_source 为 doubanmusic 时使用。",
            "douban_sort": "豆瓣音乐排序：U 综合、S 评分、R 最新、O 最热。",
        }
        for field_name, description in descriptions.items():
            field_schema = query_schema["properties"].get(field_name)
            if isinstance(field_schema, dict):
                field_schema["description"] = description
    if operation_id == "plugin.config.update" and body_schema is not None:
        body_schema["minProperties"] = 1
        body_schema["description"] = (
            "完整的插件配置对象。先调用 plugin.config.get，复制返回的配置模型，"
            "仅修改目标键后提交完整对象；只有需要删除某个键时才省略该键。"
            "更新成功后会重新初始化插件，并刷新命令、任务和路由。"
        )
    if operation_id == "system.upgrade.dev":
        return (
            {
                "type": "string",
                "const": "dev",
                "description": "固定为 dev；正式版本更新须分别调用检查、下载和安装操作。",
            },
            True,
        )
    return body_schema, None


def _resolve_openapi_schema(
    schema: Mapping[str, Any],
    components: Mapping[str, Any],
) -> Mapping[str, Any]:
    """
    解析响应中使用的本地 OpenAPI 引用与可空联合。
    """
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        resolved = components.get(reference.rsplit("/", 1)[-1])
        if isinstance(resolved, Mapping):
            return _resolve_openapi_schema(resolved, components)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            if not isinstance(alternative, Mapping):
                continue
            resolved = _resolve_openapi_schema(alternative, components)
            if resolved.get("type") != "null":
                return resolved
    return schema


def _response_data_schema(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
) -> Mapping[str, Any]:
    """
    读取统一响应的 data schema，供 MCP 标注集合输出合同。
    """
    responses = operation.get("responses")
    if not isinstance(responses, Mapping):
        return {}
    success = responses.get("200") or responses.get(200)
    if not isinstance(success, Mapping):
        return {}
    content = success.get("content")
    if not isinstance(content, Mapping):
        return {}
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return {}
    raw_schema = media.get("schema")
    if not isinstance(raw_schema, Mapping):
        return {}
    response_schema = _resolve_openapi_schema(raw_schema, components)
    properties = response_schema.get("properties")
    if not isinstance(properties, Mapping):
        return response_schema
    data_schema = properties.get("data")
    if not isinstance(data_schema, Mapping):
        return response_schema
    return _resolve_openapi_schema(data_schema, components)


def _collection_response_contract(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
) -> dict[str, Any] | None:
    """
    从 OpenAPI 响应声明提取列表或结构化分页结果的机器可读合同。
    """
    responses = operation.get("responses")
    success = responses.get("200") if isinstance(responses, Mapping) else None
    if not isinstance(success, Mapping) and isinstance(responses, Mapping):
        success = responses.get(200)
    headers = success.get("headers") if isinstance(success, Mapping) else None
    header_names = {str(name).lower() for name in headers} if isinstance(headers, Mapping) else set()
    data_schema = _response_data_schema(operation, components)
    if data_schema.get("type") == "array":
        has_total = "x-total-count" in header_names
        parameters = operation.get("parameters")
        query_parameters = (
            {
                str(parameter.get("name")): parameter
                for parameter in parameters
                if isinstance(parameter, Mapping) and parameter.get("in") == "query"
            }
            if isinstance(parameters, list)
            else {}
        )
        compatibility_parameters = [
            query_parameters.get("page"),
            query_parameters.get("count"),
        ]
        native_window_names = {"limit", "offset", "page_size", "max_results"}
        has_native_window_default = False
        for name in native_window_names:
            parameter = query_parameters.get(name)
            if not isinstance(parameter, Mapping):
                continue
            if parameter.get("required", False):
                has_native_window_default = True
                break
            schema = parameter.get("schema")
            if isinstance(schema, Mapping) and schema.get("default") is not None:
                has_native_window_default = True
                break
        defaults_to_unpaginated = (
            all(
                isinstance(parameter, Mapping)
                and not parameter.get("required", False)
                and isinstance(parameter.get("schema"), Mapping)
                and "default" not in parameter["schema"]
                for parameter in compatibility_parameters
            )
            and not has_native_window_default
        )
        return {
            "body_shape": "list",
            "result_count_field": "collection.result_count",
            "total_count_field": "collection.total_count" if has_total else None,
            "default_pagination": ("unpaginated" if defaults_to_unpaginated else "endpoint-defined"),
        }
    data_properties = data_schema.get("properties")
    if not isinstance(data_properties, Mapping) or "total" not in data_properties:
        return None
    items_field = next(
        (name for name in ("items", "list") if name in data_properties),
        None,
    )
    if items_field is None:
        return None
    return {
        "body_shape": "page_object",
        "items_field": f"data.{items_field}",
        "total_count_field": "data.total",
        "default_pagination": "endpoint-defined",
    }


def _collection_response_guidance(contract: Mapping[str, Any]) -> str:
    """
    把集合输出契约转换为 oneOf 分支中的中文说明。
    """
    if contract.get("body_shape") == "page_object":
        return (
            f"集合响应：条目保留在 {contract['items_field']}，精确总数保留在 "
            f"{contract['total_count_field']}。仅查询数量时，请使用文档允许的最小分页并读取总数；"
            "不要仅因条目数据被截断就查询数据库。"
        )
    if contract.get("total_count_field"):
        if contract.get("default_pagination") != "unpaginated":
            return (
                "集合响应：data 保持列表形式，端点原有的分页或条数限制默认值继续生效。"
                "网关成功响应会增加 collection.result_count 和精确的 collection.total_count。"
                "仅查询数量时，请使用合法的最小分页并读取 collection.total_count；"
                "不要仅因条目数据被截断就查询数据库。"
            )
        return (
            "集合响应：data 保持列表形式；同时省略 page 和 count 可保留原有完整结果。"
            "网关成功响应会增加 collection.result_count 和精确的 collection.total_count。"
            "查询数量或摘要时，请传入 page=1 和 count=1，再读取 collection.total_count；"
            "不要仅因条目数据被截断就查询数据库。"
        )
    return (
        "集合响应：data 保持列表形式，网关成功响应会增加 collection.result_count。"
        "端点或其上游来源未提供总数时，省略 collection.total_count。"
    )


def build_api_mcp_input_schema(
    *,
    openapi: Mapping[str, Any],
    routes: Mapping[str, Any],
    specs: Sequence[Any],
) -> dict[str, Any]:
    """
    构建全部白名单 operation 的完整 MCP oneOf 输入契约。

    :param openapi: 当前宿主 API 的 OpenAPI 文档，包含参数、请求模型及响应定义
    :param routes: 操作标识到固定 HTTP 方法及路径的登记映射
    :param specs: 与路由集合完全对应的权限和副作用策略
    :return: 包含中文说明、独立模型定义及全部操作分支的 JSON Schema
    :raises ValueError: 操作集合不一致，或缺少路由、模型及必要说明
    """
    components = openapi.get("components", {}).get("schemas", {})
    if not isinstance(components, Mapping):
        components = {}
    definitions: dict[str, Any] = {}
    spec_by_id = {spec.operation_id: spec for spec in specs}
    if set(spec_by_id) != set(routes):
        raise ValueError("API operation 策略与路由注册表不一致")

    branches: list[dict[str, Any]] = []
    for operation_id in sorted(routes):
        route = routes[operation_id]
        operation = _resolve_openapi_operation(operation_id, route, openapi)
        summary = OPERATION_DESCRIPTIONS.get(operation_id)
        if not summary:
            raise ValueError(f"MCP operation guidance is missing: {operation_id}")
        parameters = operation.get("parameters")
        if not isinstance(parameters, list):
            parameters = []
        path_schema = _parameter_object_schema(
            parameters,
            location="path",
            components=components,
            definitions=definitions,
        )
        query_schema = _parameter_object_schema(
            parameters,
            location="query",
            components=components,
            definitions=definitions,
        )
        body_schema, body_required = _request_body_schema(
            operation,
            components=components,
            definitions=definitions,
        )
        body_schema, required_override = _apply_operation_overrides(
            operation_id,
            query_schema,
            body_schema,
        )
        if required_override is not None:
            body_required = required_override

        properties: dict[str, Any] = {
            "operation_id": {"type": "string", "const": operation_id},
        }
        required = ["operation_id"]
        if path_schema is not None:
            path_schema["description"] = (
                f"{operation_id} 的资源标识参数。{summary}仅使用下列已声明字段。"
            )
            properties["path_params"] = path_schema
            if path_schema.get("required"):
                required.append("path_params")
        if query_schema is not None:
            query_schema["description"] = (
                f"{operation_id} 的筛选和控制参数。{summary}仅使用下列已声明字段。"
            )
            properties["query"] = query_schema
            if query_schema.get("required"):
                required.append("query")
        if body_schema is not None:
            body_schema.setdefault(
                "description",
                f"{operation_id} 的请求内容。{summary}严格使用下列类型和字段。",
            )
            properties["body"] = body_schema
            if body_required:
                required.append("body")

        spec = spec_by_id[operation_id]
        collection_contract = _collection_response_contract(operation, components)
        collection_guidance = (
            _collection_response_guidance(collection_contract) if collection_contract is not None else ""
        )
        branch = {
            "type": "object",
            "title": operation_id,
            "description": (
                f"{summary}请求方法：{route.method}；路径：{route.path}。"
                f"操作效果：{spec.effect.value}；身份范围：{spec.identity_scope.value}。"
                f"所需角色：{spec.required_role.value}。{collection_guidance}"
            ),
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        if collection_contract is not None:
            branch["x-moviepilot-collection"] = collection_contract
        branches.append(branch)

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "moviepilot_api",
        "type": "object",
        "description": (
            "选择与 operation_id 匹配的 oneOf 分支，仅发送该分支声明的字段。"
            "集合分支还通过 x-moviepilot-collection 说明额外返回的集合元数据。"
        ),
        "properties": {
            "operation_id": {"type": "string", "enum": sorted(routes)},
            "path_params": {"type": "object"},
            "query": {"type": "object"},
            "body": {},
        },
        "required": ["operation_id"],
        "oneOf": branches,
    }
    if definitions:
        for model_name, definition in definitions.items():
            description = MODEL_DESCRIPTIONS.get(model_name)
            if not description:
                raise ValueError(f"MCP model guidance is missing: {model_name}")
            definition["description"] = description
        schema["$defs"] = definitions
    return _apply_field_descriptions(schema)


__all__ = ["build_api_mcp_input_schema"]
