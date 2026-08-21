# v3-python 复审报告（2026-07-02）

> **范围**：v3-python 主线（HEAD 树 == `origin/v3-python`，PR #97/#98/#99 已合入；本地分支 `fix/p1-post-message-leak` 工作树与主线一致）。
> **方法**：多 agent workflow 复审 —— ①核对上一轮（`62eecb38`，`docs/architecture-audit-v3python-2026-06-29.md`）35 confirmed + 17 needs-scoping 在当前代码的状态；②14 子系统并行发现新缺陷 → 每条独立 skeptic 默认证伪裁决。
> **规模**：两遍独立发现（首轮 47 agent + 因限流补跑 48 agent，共约 760 万 token）。finder 非确定性使两轮各有侧重，**取并集**覆盖更全；6 条最高危发现经主审逐行独立复读坐实。
> **上一轮已修项不重复上报**；本轮只报"修复不完整/遗漏边"与全新问题。

---

## 一、执行摘要

**总体判断：安全正确性明显改善，但暴露出 1 个新 CRITICAL、1 个新 HIGH DoS、1 个由上轮修复引入的 HIGH 回归。**

上一轮 P0/P1 的信任边界与隐私隔离整改**确已闭合**：Zip Slip、runtime 路径穿越、clone suffix、scheduler 协程黑洞、post_message 隐私泄漏族（chain 层 + 全部 9 个内建通知渠道 fail-closed 契约）均经核对为 `fixed`，无残留裸奔边。这是实打实的进步。

但本轮发现三件必须优先处理的事：

1. **[CRITICAL] 图片代理缓存键路径穿越 → 任意文件读取**（`utils/security.py:829` + `helper/image.py:186` + `core/cache.py` FileBackend）。`sanitize_url_path` 用 `quote()`（默认 `safe='/'`）不消除 `../`，缓存 key 直接拼进 `base/images/key` 无 `is_within` 兜底。任一**已登录用户**构造 `imgurl=https://<允许域>/../../../../config/user.db&cache=true` 即可读取容器内带扩展名任意文件（`app.env` 的 SECRET_KEY、`user.db` 全量口令哈希/cookie/token）→ 机密全泄、越权到完全接管。默认文件缓存后端即命中。

2. **[HIGH] `_ThinkTagStripper.process` 死循环 → 全服务 DoS**（`agent/__init__.py:180-194`）。not-in-think 分支处理 `<think>` 不完整前缀后**缺** in-think 分支（210 行）那样的 `while` break，一条以 `<` 结尾的流式 token 即令事件循环 100% CPU 占死、只能 kill 进程。启用流式（消息编辑渠道或 `AI_AGENT_VERBOSE`）即可触发。

3. **[HIGH] 存储凭据越权泄露**（`api/endpoints/system.py:815-827` + `:57-68`）。`get_public_setting` 只 `Depends(get_current_active_user_async)`（非超管即可），而 `_PUBLIC_SYSTEM_CONFIG_KEYS` 白名单含 `SystemConfigKey.Storages`，命中后原样返回 `SystemConfigOper().get(Storages)` **无脱敏**。任一非超管登录用户 `GET /api/v1/system/setting/public/Storages` 即可拿到全部网盘 OAuth token / alist 账号密码 / SMB 明文密码 → 越权直取受害者云存储。多用户部署下触发（单用户部署仅超管账号则不适用）。

4. **[HIGH·回归] 关停竞态死锁**（`scheduler.py:725` + `startup/lifecycle.py:121`）。上一轮 PR-B（#3）给协程 job 加的 `.result()` 阻塞 APScheduler worker 线程等主循环跑协程；关停时 `lifespan` **同步**调 `stop_scheduler()`→`shutdown(wait=True)` 在主循环 join worker → worker 等主循环、主循环等 worker，死锁。触发窗口=协程 job（如插件市场刷新，不可达时 HTTP 超时数十秒）正在执行时收到关停信号 → lifespan 永久挂起、逆序关闭全跳过、SIGKILL 丢数据。**这条修正了旧结论"`.result()` 阻塞 worker 无死锁（已取证）"——旧取证只验了常态运行、漏了关停并发路径。**

其余（两轮并集去重后）：**约 23 条 confirmed 新发现**（1 critical / 4 high / 7 medium / 11 low）+ **10 条 needs-scoping**。仍存活的旧 backlog 11 项（#5/#6 subscribe 锁、#9-sync systemconfig 锁内 I/O、#10~#14、#16 pickle、#17 结构债、site UNIQUE 等）。

**推进次序**：`CRITICAL 路径穿越 + HIGH 存储凭据泄露` → `HIGH（think 死循环 + 关停回归 + Alist 截断下载数据丢失）` → `MED 安全（停用超管 JWT、限流 OOM、Alist 文件名穿越）` → `旧 backlog 并发正确性（#5/#6/#9-sync/#11/#12）` → `结构债`。**切勿在已知竞态上先做大重构。**

---

## 二、已修复项闭合确认（recon = fixed）

| 项 | 位置 | 结论 |
|---|---|---|
| #1 Zip Slip | `helper/plugin.py` `plan_release_zip_extraction`（同/异步双路径均消费已 `is_within` 校验的 planned 列表，全仓无 `extractall`） | **fixed** |
| #2 runtime 路径穿越 | `agent/runtime.py:734` `_resolve_relative_path`（拒绝绝对路径 + `is_within` 校验；唯一入口 `_resolve_optional_paths`） | **fixed** |
| #15 clone suffix | `helper/plugin_manager.py:1650`（`1≤len≤20 & isascii & isalnum` 白名单 + `is_within` 纵深） | **fixed** |
| #3 scheduler 协程黑洞 | `scheduler.py:725` `.result()` 阻塞收口异常、`__finish_job` 协程结束后复位（**但见新发现 H2 关停死锁回归**） | **fixed（异常黑洞）/ 引入新回归** |
| #4 post_message 隐私泄漏族 | `chain/__init__.py` post/async 完全镜像：actions 规范化 + None 守卫 + fail-closed（仅 `all` 广播）+ `if not send_message.targets: continue` 集中兜底 | **fixed** |
| 9 个内建通知渠道 fail-closed 契约 | telegram/wechat/wechatclawbot/slack/qqbot/vocechat/discord/webpush/feishu 均 `if not userid and targets is not None:` 解析→取不到即 return | **fixed（无未覆盖下游）** |
| systemconfig falsy（PR#98） | `db/systemconfig_oper.py:46/83` `if value is not None`（0/False/[]/{}/"" 不再误删） | **fixed** |

> 通知隔离契约去中心化在 ~10 渠道各自实现——**新增渠道 post_message 必须 `targets is not None`+解析不出即 return，勿用真值判断 `and targets:`（会放过 `{}`）**。

---

## 三、新发现 · Confirmed（两轮并集去重后约 23：1 critical / 4 high / 7 medium / 11 low）

### 🔴 CRITICAL

**C1. 图片代理缓存键路径穿越 → 任意文件读取（app.env/user.db 泄露）** — `utils/security.py:829-843`（+ `helper/image.py:186`、`core/cache.py` (Async)FileBackend get/set）
- 机制：`sanitize_url_path` = `path.lstrip('/')` + `quote()`（默认 `safe='/'`，`../` 原样保留）→ `_prepare_cache_path` 有扩展名（`.env/.db/.py`）跳过补 `.jpg` → `AsyncFileBackend.get` `AsyncPath(base)/region/key` 无 `is_within`，OS 解析 `../` 后 `aiofiles.open` 读原字节。`is_safe_image_url_async` 只校验 scheme+域名 allowlist+DNS，**从不校验 path**；本地缓存命中在任何远端请求之前、且**不过 `_validate_image`**，攻击者无需控制远端。
- 影响：任一持 resource token 的登录用户读取 `/config/app.env`（SECRET_KEY→可伪造 admin JWT）、`/config/user.db`（口令哈希/cookie/token 全库）；默认文件缓存后端即命中。
- 修复：`sanitize_url_path` 内 `'/'.join(seg for seg in path.split('/') if seg not in ('','.','..'))` 后再 `quote`；(Async)FileBackend get/set/delete 落盘前 `SystemUtils.is_within(base/region, (base/region/key).resolve())` fail-closed；`image.py` 缓存命中分支补 `_validate_image`。**sync+async 两后端同时加**。

### 🟠 HIGH

**H1. `_ThinkTagStripper.process` 死循环 → 单进程 DoS** — `agent/__init__.py:180-194`
- 机制：not-in-think 分支对 `<think>` 不完整前缀 `self.buffer = self.buffer[-i:]` 后 `partial_match=True`，但缺 in-think 分支（210 行）的 `while` break；buffer 恰为裸前缀（`len==i`）时 `len>i` 为假不输出、buffer 不变，`while self.buffer:` 无限自旋。`process()` 内无 `await`，占死事件循环。
- 影响：启用流式（消息编辑渠道 / `AI_AGENT_VERBOSE`）时一条以 `<`/`<t`/…/`<think` 结尾的 token 即触发全服务挂起，CPU 打满，需 kill 进程。
- 修复：not-in-think 分支部分前缀处理后补 `break`（与 210 行对称）；186 行已先输出 `buffer[:-i]`，break 只保留待补全尾部，无吞字。

**H2.〔回归〕关停竞态死锁：协程 job `.result()` 与主循环 `shutdown(wait=True)` 互等** — `scheduler.py:703-725, 998-1009` + `startup/lifecycle.py:121`
- 机制：协程 job 走 `run_coroutine_threadsafe(coro, global_vars.loop).result()`（无 timeout）阻塞 APScheduler worker 线程等主循环执行协程；关停时 `lifespan` finally **同步**（非 await）调 `stop_scheduler()`→`shutdown(wait=True)` 阻塞主循环 join worker。二者同一循环 → 死锁。
- 影响：关停信号落在协程 job 执行窗口内（插件市场不可达时 HTTP 超时数十秒，窗口大）即触发；lifespan 永久挂起，`backup_plugins`/`stop_plugins`/关库全跳过，SIGKILL 丢在途数据。
- 修复：`lifecycle.py` 的 `stop_scheduler()` 改 `await asyncio.to_thread(stop_scheduler)`——主循环在 `shutdown(wait=True)` 等待期间仍能回收挂起协程使 `.result()` 返回，解开互等（保留 #3 异常收口）。**勿改 `wait=False`（丢在途 job）或 `.result(timeout=)`（退回 #3 黑洞）。**

**H3. 存储凭据越权泄露：非超管用户读取全部网盘/存储凭据** — `api/endpoints/system.py:815-827, 57-68`（安全）
- 机制：`get_public_setting` 依赖 `Depends(get_current_active_user_async)`（只校验 `is_active`，不校验超管）；`_PUBLIC_SYSTEM_CONFIG_KEYS`（:57-68）把 `SystemConfigKey.Storages` 纳入白名单，命中后 `value = SystemConfigOper().get(Storages)` 原样返回、无脱敏。而 Storages config dict 存放凭据（alist username/password/token、smb password、u115/alipan refresh_token/access_token）。
- 影响：非超管账号登录后 `GET /api/v1/system/setting/public/Storages` 直取全部存储后端凭据 → 越权接管受害者云存储。多用户部署触发（单用户仅超管则不适用）。
- 修复：从 `_PUBLIC_SYSTEM_CONFIG_KEYS` 移除 `Storages`；若前端普通用户需存储列表，返回前剥离 config（仅留 type/name）或改走超管口。

**H4. Alist.download 失败/中断返回残缺文件路径 → 媒体库落入截断文件、move 模式删源不可恢复** — `modules/filemanager/storages/alist.py:677-691`（正确性）
- 机制：流式写盘循环网络中断时 `except` 分支 `if local_path.exists(): return local_path`（688-689），把部分字节文件当成功返回；末尾还有无条件 `return local_path`（691）。对比 alipan/u115/smb/rclone 失败分支均 `local_path.unlink() + return None`——**Alist 是唯一失败仍返回路径的后端**。`transhandler` 拿到真值 Path 不校验大小即 `SystemUtils.move`，`move` 模式随后 `source_oper.delete` 删源。
- 影响：Alist 大文件下载断流（云盘常见）被判成功，截断/损坏媒体入库；move 模式源被删 → 不可恢复的数据损坏与丢失。
- 修复：失败分支 `if local_path.exists(): local_path.unlink(); return None`，删末尾无条件 `return local_path`，仅流循环正常结束才返回路径。

### 🟡 MEDIUM

**M1. 停用的超级管理员仍可用现有 JWT 访问全部超管端点** — `db/user_oper.py:61-84`（安全）
- `get_current_active_superuser(_async)` 只 `Depends(get_current_user)` 且只查 `is_superuser`，**漏 `is_active`**（对比 `get_current_active_user:56-57` 有查）。停用超管既有 JWT 过期前（最长 8 天）仍可访问 user CRUD/storage/plugin/system 全部超管端点——最高权限账号反而无法即时吊销。
- 修复：两函数改 `Depends(get_current_active_user/_async)`，或函数体补 `if not current_user.is_active: raise HTTPException(403)`。

**M2. 认证限流器按 (ip:username) 无界累积 → 内存耗尽 DoS** — `utils/limit.py:452-479`（安全）
- `_registry` dict 每唯一 `(ip:username)` 一 `WindowRateLimiter`、只增不逐（`clear()` 仅测试用）。同一 IP 变换 username 请求登录端点即无界累积 → 单进程 OOM 全站崩。次生：同 IP 跨账号 password-spray 不被总量限流。
- 修复：`_registry` 加 LRU 上限并在 `_lock` 内惰性清扫已整体过期的 key（仅逐完全过期项，勿逐窗口内 key）。

**M3. AUTO_DOWNLOAD_USER 指定用户白名单对整数 userid 渠道（Telegram）永久失效** — `chain/media_interaction.py:1460-1467`（正确性）
- `any(userid == user for user in auto_download_user.split(','))`：右操作数恒 str，Telegram `userid` 为 int（pydantic smart-union 保留 int），`int==str` 恒 False。配具体数字 ID 时 Telegram 用户搜索后永不自动择优下载，仅 `'all'` 生效，与字符串 ID 渠道行为不一致且难排查。
- 修复：`any(str(userid) == u.strip() for u in ...)`（`'all'` 分支保持在字符串化之前）。

**M4. provider.py 模型列表接口每次调用泄漏 httpx.AsyncClient / genai.Client** — `agent/llm/provider.py:1703-1728, 1669`（性能）
- `_list_models_from_openai_compatible`/`_list_models_from_google` 构造 client 后 `return` 从不 `close()/aclose()`（孪生 `helper.py` 已显式关闭，其余同文件全用 `async with`，属遗漏）。`resolve_runtime`(2527 `if model:`) 对**每次建模**调 `list_models` → 每条 Agent 消息泄漏一套连接池 + 一次未缓存 `/models` 网络请求。
- 修复：两函数 try/finally 中 `await client.close()`/`await client.aio.aclose()`（或 `async with`）；异常路径也关。

**M5. 远程目录监控快照持久化用增量分片覆盖全量基准 → 已整理文件周期性重整理** — `monitor.py:736-745, 787`（正确性）
- 远程存储启用 folder-modtime 增量时，某静默目录一旦变化，该目录历史文件被整批当新增回灌 `__handle_file→do_transfer`。与已知 #8（全失败→空快照覆盖）不同：本条在**正常增量运行**下由"覆盖而非合并"触发。
- 修复：落盘前并集合并 `persisted = {**old, **new}` 再 `save_snapshot`；`snapshot_time` 基于合并集保持单调。
- 补跑更精确定位了同族机制：`StorageBase.snapshot`（`storages/__init__.py:285-294`）在 `snapshot_check_folder_modtime`（默认 True）下对未变目录 `return` 整棵子树 → 产出的是**部分文件集**，`monitor.py:787` 原样覆盖基准；未变目录历史文件从基准消失，其目录再变动时被判 added 重整理。修复同上（并集合并 / 基准快照走全量 `last_snapshot_time=0`）。

**M6. Alist.download 用远端 `fileitem.name` 拼接落盘路径，绕过 `_build_download_path` 安全校验** — `modules/filemanager/storages/alist.py:671-674`（安全）
- 机制：`local_path = path / fileitem.name`，`fileitem.name` 来自 Alist `/api/fs/list` 返回的 `item['name']`（远端可控）。`StorageBase._build_download_path→_safe_download_name` 取 `PurePosixPath(...).name` 拒绝 `''/'.'/'..'` 并 `resolve().relative_to(path)` 兜底；smb/rclone/u115/alipan 均已接入，**唯 Alist 未接**。被投毒的 AList 服务返回含 `..`/分隔符的 name 即可写出目标目录（与 H4 同处一方法，穿越+残缺叠加）。
- 修复：`local_path = self._build_download_path(fileitem, path or settings.TEMP_PATH); if not local_path: return None`。

### ⚪ LOW（10 条，摘要）

| # | 位置 | 问题 | 修复要点 |
|---|---|---|---|
| L1 | `chain/subscribe.py:1698-1775` | `check()` 无锁读改写洗版状态列，与持 `_rlock` 的 search/match 并发**丢失更新**（episode_priority 回退→重复下载）。自评 high→裁 medium（限 best_version TV + 洗版写并发重叠、部分自愈）。 | 把"重取行→重算优先级→update"窄操作收进 `_rlock` 临界区并 re-read；或 episode_priority 改按集 JSON 合并 |
| L2 | `chain/transfer.py:819-821` | `_total_num` 单调 `max` 永不复位，后续小批次进度百分比被历史峰值稀释（纯 UX） | 批次收尾分支（889-891，已在 task_lock）连同 processed/fail 一起 `_total_num=0` |
| L3 | `chain/tmdb.py:152-157` | `get_random_wallpager` `while True` 在整页无 backdrop 时死循环（同步版挂线程池、async 版潜伏） | 改有界 `candidates=[...]; random.choice(candidates) if candidates else None` |
| L4 | `agent/__init__.py:1098-1123` | `process()` 就地改 memory 缓存消息列表，失败/取消后遗留无应答 HumanMessage 污染下轮上下文 | `get_agent_messages` 返回 `list(memory.messages)` 浅拷贝 |
| L5 | `agent/llm/provider.py:2466-2493` | `_resolve_chatgpt_oauth` 无锁 read-refresh-write，临期并发重复用同一 refresh_token→轮换失效间歇鉴权失败（#12 刷新路径补充边） | per-provider `asyncio.Lock` 包 get→refresh→save + 进锁双检 expires_at |
| L6 | `core/event/manager.py:86-96, 235-236` | `check()`/`visualize_handlers()` 无锁迭代活订阅字典，与热重载增删并发抛 `RuntimeError: dict changed size` | 对齐 dispatch：锁内 `list(...)` 快照后锁外遍历 |
| L7 | `db/userconfig_oper.py:33` | `set()` 用 `if value:` 真值判断误删 falsy 合法配置（缓存与库发散） | 改 `if value is not None:`（对齐 systemconfig；本 oper 无 async_set） |
| L8 | `db/systemconfig_oper.py:40` | 同步 `set()` 先写内存缓存后写 DB，DB 写失败遗留粘滞错值 | 缓存写下移到 DB 成功后（镜像 async_set line 91 次序） |
| L9 | `db/site_oper.py:78-79` | `SiteOper.update()` 缺 None 守卫，sid 不存在时 `None.update` 崩 | 加 `if not site: return None`（对齐 SubscribeOper） |
| L10 | `helper/torrent.py:102-111` | `download_torrent` 手工重定向循环无上限且 `Location` 缺失 KeyError | 最大跳数（10）计数 break + `loc = headers.get('Location'); if not loc: break` |

> 另 `scheduler.py:947-955/1002-1010` `list()` 与 `stop()/init()` 分持不同锁的 `_scheduler` 读用竞态（偶发 `/schedule` 500，low）：`list()` 开头把 `scheduler=self._scheduler` 绑局部变量全程用 + `try/except`。

---

## 四、新发现 · Needs-Scoping（10，真缺陷·触发依赖配置/部署）

| 位置 | 问题 | 触发条件 / 修复 |
|---|---|---|
| `chain/__init__.py:1364-1392` | `async_post_message` 隔离分支在事件循环内做**同步阻塞 DB 读**（`UserOper.get_settings`），抵消 async 价值 | 无 userid 且配 mtype 隔离的系统通知；DB 慢盘放大。修：`await run_in_threadpool(...)` 或新增 `async_get_settings` |
| `chain/__init__.py:1400-1402` | post/async 对 SUPERUSER **重复下发**（`admin,user` 顺序 + username==SUPERUSER 收两条） | 无 userid + mtype='admin,user' + 消息 username 恰为超管。修：admin 分支加去重守卫 |
| `chain/download.py:127-129` | `_resolve_media_download_dir` 缺目录返回裸 `None`→`download_subtitle` 解包 `TypeError`→500（本应优雅报错） | POST /download/subtitle 无 save_path + 无匹配目录（新装常见）。修：改返回二元组 `return None, None` |
| `chain/transfer.py:1084-1093` | 批次入库通知无去重标记，`TRANSFER_THREADS>1` 末尾任务并发命中 `is_finished`→重复通知 + 空文件清单 | 并发整理开启。修：`job_lock` 内对 `__mediaid__` 设 notified CAS 标志 |
| `modules/douban/__init__.py:154, 254` | 按 ID 识别 `meta=None && mtype=None` 时 `mtype or meta.type` 抛 `AttributeError`（TMDB 有兜底、豆瓣独缺） | 豆瓣 ID 识别且类型未知。修：`mtype or (meta.type if meta else None)` |
| `modules/filemanager/storages/__init__.py:285-289` | 增量快照按祖先目录 mtime 剪枝整棵子树，**深层新增/修改文件静默漏采** | 远程存储启用 folder-modtime 增量 + 深于 mon_path 一层的变更。修：仍递归子目录、仅对未变目录复用文件层缓存 |
| `modules/filemanager/storages/alist.py:290` | `__parse_timestamp` 无异常兜底，单个非法 `modified` 中止整目录 `list()` | 后端返回异常时间串。修：try/except 返回 0/None + `.get()` 容错（与上一条一并考虑） |
| `helper/directory.py:72` | `get_dir` 对 `download_path=None` 目录调 `is_relative_to(None)` 抛 `TypeError`→500 | 目录配置存在空 download_path 条目。修：`[d for d in dirs if d.download_path and src_path.is_relative_to(d.download_path)]` |
| `modules/feishu/feishu.py:159-206` | 飞书多实例对 `lark_oapi.ws.client` 模块全局 `loop/_select` 打补丁跨实例互踩，一个机器人收不到消息/跨事件循环崩溃（同 #14 Telegram 家族） | 部署 ≥2 个飞书渠道。修：绑定式补丁用实例自持 `self._ws_loop`（子类化/`functools.partial` 注入），或收敛到单后台 loop 多路复用 |
| `helper/plugin_manager.py:112-120` | `reload_plugin` 并发下 `start()` 常规分支锁外建实例、写 `_running_plugins` 无二次校验 → 与并发 `stop()` 竞态复活已下线插件（僵尸实例） | 并发同 pid 热重载（文件监控重载 + API/agent 切换）。修：L120 写前锁内二次校验 `pid in _plugins`，否则丢弃并 stop 刚建实例；或 per-pid 串行锁 |

---

## 五、仍存活的旧 backlog（recon = live/partial）

| ref | 现状 | 位置/要点 |
|---|---|---|
| #5/#6 subscribe 锁超时续跑 + 持锁 `sleep(60-300s)` | **live** | `subscribe.py:981-986`（超时仅 warn 后无锁续跑）、`1009-1013`（持锁逐订阅 sleep）；match 同构。并发正确性最危险项，须先于结构拆分 |
| #9 systemconfig 同步 `set()` 锁内 DB I/O | **partial** | `systemconfig_oper.py:36` `with self._rlock:` 内含 DB 写；async_set 已加固，**唯同步 set 仍暴露** |
| #10 upload_avatar 无大小限制 DoS | **live** | `api/endpoints/user.py:116` 同步 `file.file.read()` 无上限 → 阻塞事件循环 + 内存放大 |
| #11 agent 吞 CancelledError | **live** | `agent/__init__.py:1392` catch 后 `return` 无 re-raise；stop/heartbeat `await worker` 卡满 60s idle |
| #12 save_auth 无锁 read-modify-write | **live** | `agent/llm/provider.py:1385-1406` 跨 await 无锁，并发 OAuth 后写覆盖丢 token（另见 L5 刷新路径） |
| #13 themoviedb 哨兵语义反转 | **live** | `modules/themoviedb/__init__.py:960` 非 themoviedb 源 `return None` 反让调用方继续抓图 |
| #14 telegram 类级 dict + apihelper 全局 | **live** | `modules/telegram/telegram.py:40-46`（类字典多实例串味）、`76-85`（apihelper 模块全局 last-write-wins） |
| #16 pickle.loads RCE（部署条件型） | **live** | `core/redis.py:73`、`chain/__init__.py:66/79` 裸 `pickle.loads` 无 HMAC/限制 |
| #7 monitor modify_job 误用 | **live** | `monitor.py:794` `modify_job(trigger='interval')` 传字符串抛 TypeError 被吞，动态间隔永久失效。应改 `reschedule_job` |
| #8 monitor 空快照覆盖 | **live** | `monitor.py:786-787` 全失败时空 `{}` 覆盖基准 → 下轮全量重处理（无全失败守卫） |
| site.domain UNIQUE / SiteOper.update None | **live** | `db/models/site.py:19` 仅 index 无 unique；`db/site_oper.py:74` 无 None 守卫（= L9） |
| core/event SystemError 递归 + 无界队列 | **live** | `core/event/manager.py:40` `PriorityQueue()` 无 maxsize；`:668` `__handle_event_error` 无 SystemError 递归守卫 |
| core/cache MemoryBackend 类变量 | **live** | `core/cache.py:366/368` `_lock`/`_region_caches` 类变量，首个创建者决定 region 类型（低危潜伏） |

---

## 六、架构与结构债

- **规模**：~180K 行；**75 个文件 >800 行**。上帝类头部：`transfer.py 3447` / `subscribe.py 3413` / `helper/plugin.py 2778` / `agent/llm/provider.py 2744` / `search.py 2378` / `tmdbapi.py 2238` / `wechatclawbot 2219` / `agent/__init__.py 1917` / `message.py 1900` / `media.py 1900` / `chain/__init__.py 1883`。
- **子系统 LOC**：modules 58.5K / plugins 47.4K / agent 28.7K / chain 24.0K / helper 15.7K / api 13.1K / core 10.5K / utils 7.0K / db 6.1K。
- **#17 结构债仍在**（`subscribe.py` 3413 上帝类 + `transfer.py` name-mangling 跨类私有访问）：`check()/search()/match()` 共享 `_rlock` 但 check 不遵守（L1/#5/#6 类竞态难单独修）。**方案不变：先修 #5/#6/L1 并发正确性，再按域拆 SubscribeChain（BestVersionStateMachine / SubscribeSearchService / SubscribeMetadataService），严禁在已知竞态上做结构搬迁。**
- **by-design 边界不动**（勿当 bug）：scheduler 双锁防死锁、`run_module` 字符串分发 + `raise_exception` 透传契约、渠道能力双轨、惰性 import、单进程 `Server.run()`（`workers` 不生效）、`WeakSingleton` 已带锁、`GET /system/global` 的 `token=="moviepilot"` 是登录前公开端点。

---

## 七、优先级路线图

**P0（安全/DoS，立即，小改面）**
- `C1` 图片代理路径穿越：`sanitize_url_path` 分段过滤 `..` + (Async)FileBackend `is_within` fail-closed + 缓存命中补 `_validate_image`（sync+async 双改）。
- `H3` 存储凭据泄露：`_PUBLIC_SYSTEM_CONFIG_KEYS` 移除 `Storages`（或返回剥离 config 的精简结构）。
- `H1` ThinkTagStripper 死循环：补 not-in-think 分支 `break`。
- `H2` 关停死锁回归：`stop_scheduler()` 改 `await asyncio.to_thread(...)`。
- `M1` 停用超管 JWT：超管依赖补 `is_active`。
- `M2` 限流器无界累积：`_registry` LRU + 惰性清扫。

**P1（并发/数据正确性，核心；先于结构拆分）**
- `H4` Alist 截断下载数据丢失 + `M6` Alist 文件名穿越（同一 `alist.py` download 方法，一并修：失败 unlink+return None、接入 `_build_download_path`）。
- 旧 #5/#6 subscribe 锁（超时即 return + sleep 移出锁"释放-睡眠-重读-重获取"）。
- `L1` check() 丢失更新（窄操作纳入 `_rlock` + re-read）——与 #5/#6 同族，同批修。
- 旧 #9-sync systemconfig 锁内 I/O（DB I/O 移出 `_rlock`，对齐 async_set）。
- 旧 #11 agent CancelledError re-raise + heartbeat 收口；旧 #12 + `L5` save_auth/OAuth 刷新 asyncio.Lock。
- `M5` monitor 增量覆盖→并集合并（含 storages 子树剪枝）；旧 #7 `reschedule_job`、#8 全失败守卫。

**P2（可用性/健壮性 + 结构债 + 部署条件型）**
- `M3` AUTO_DOWNLOAD_USER 字符串化；`M4` provider client 泄漏关闭；`L2–L10` 低危族；needs-scoping 10 项（先确认触发条件，含飞书多实例、reload 僵尸）。
- 旧 #16 pickle→JSON/HMAC（部署条件型，配 requirepass）；旧 #13/#14；site UNIQUE。
- 旧 #17 结构治理（排在 P1 并发修复之后）。

**通用**：每条 TDD（复现失败单测 RED→修 GREEN）；并发类多线程/asyncio 压测断言无重复副作用；区分仓库既有失败基线（telegram/agent_image 采集期 ERROR + smb pre-existing FAILED）只对新增/受影响用例做门禁。

---

## 八、附录

**两遍独立发现说明**：首轮 12/14 单元成功（plugin-system、modules-notify 因限流失败），补跑重跑全部 14 单元。因 finder 非确定性，两轮各有侧重、并非纯缓存回放——本报告取**并集去重**。跨两轮均命中的项（如图片路径穿越、关停死锁、限流 OOM、userconfig falsy）置信最高；单轮命中项（如首轮的停用超管 JWT `M1`、补跑的存储凭据泄露 `H3`/Alist 数据丢失 `H4`）虽只被一遍发现，但均经对抗证伪 + 主审复读，同样成立。**教训**：单遍多 agent 审计存在覆盖抖动，重要审计宜跑 ≥2 遍取并集。

**评级分歧**：图片代理路径穿越 首轮裁 `critical`、补跑裁 `high`。主审逐行复读后维持 **CRITICAL**——任一登录用户即可读 `app.env`(SECRET_KEY→伪造 admin JWT) + `user.db`(全量口令/token)，实现完全接管，高于"仅存储凭据"的 `H3`。

**refuted（并集，均经复核剔除，勿再当 bug）**：
- `core/cache.py:468-473` `MemoryBackend.clear(region=None)` 无锁迭代类级 `_region_caches` 与 set() 并发 RuntimeError —— 当前潜伏不可达/夸大。
- `helper/plugin_cloner.py:79-115` 插件分身 name/description 未过滤 f-string 拼入生成源码"代码注入 RCE" —— 复核证伪（另见旧 PR-L：`plugin_cloner` 改 AST 重写属独立低危改进，非 RCE）。
- `modules/feishu/__init__.py:146-174` 飞书 `post_medias/post_torrents_message` 缺 fail-closed —— 证伪（medias/torrents 命令回执路径 `targets` 恒 None，不经隔离广播，同 vocechat medias）。
- `modules/discord/discord.py:1155-1203` Discord 定向消息优先投公共频道、DM 排最后 —— 证伪（几乎不触发）。

- 本报告为 `docs/architecture-audit-v3python-2026-06-29.md` 的**状态刷新 + 增量发现**，非替代；旧报告的 by-design/needs-scoping 判定除本文更新处外仍有效。
