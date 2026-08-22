# 插件联邦远程标识的版本化契约

> 面向对象：MoviePilot-Frontend 维护者。本文描述后端在插件源码支持多版本并存、
> 多实例并行运行之后，如何下发 Vue 联邦远程组件的地址与标识，前端需要配合改
> 什么，以及新旧字段共存期间双方各自的取用方式。
>
> 涉及仓库：本文写在 MoviePilot（后端）仓库，前端改动在 MoviePilot-Frontend
> 仓库，本文不改前端代码，只定义契约。

## 一、背景：为什么会撞名

MoviePilot 后端支持插件源码**多版本并存**（磁盘布局
`app/plugins/<插件ID>/<版本目录>/`）与**多实例并行运行**（不同实例可各自绑定
不同版本）。带 Vue 前端的插件通过 Module Federation 以 remote 形式加载：前端
把后端下发的一个字符串标识注册为 `remotesMap` 里的键
（`__federation_method_setRemote(id, {...})`），之后按同一个标识取用组件
（`__federation_method_getRemote(id, componentName)`）。这个
`remotesMap` 是浏览器页面内的全局单一键空间。

如果两个实例各自绑定同一插件的两个不同版本，而后端下发给它们的标识相同，
后注册的会覆盖先注册的，两个实例最终会从同一个 `remotesMap` 条目里取组件，
表现为“两个版本的实例在前端拿到同一份代码”。

## 二、后端现状（改动前）

接口 `GET /api/v1/plugin/remotes` 与 `GET /api/v1/plugin/login-providers`
（认证提供方的 `remote` 子字段）下发的远程组件描述来自
`PluginProjection.remotes()` / `PluginProjection.auth_providers()`
（`app/runtime/extensions/projection/plugin.py`），字段只有三个：

```json
{ "id": "PluginId", "url": "/plugin/file/pluginid/dist/assets/remoteEntry.js", "name": "插件展示名" }
```

- `id`：插件在宿主内的实例键（`extension_id`），默认实例是裸插件标识，具名
  实例是 `插件标识@实例标识`。**这个值同时被前端多处当作“插件/实例标识”使用**：
  路由参数 `/plugin-app/:pluginId`（`plugin-app.vue`）、侧栏导航项的
  `plugin_id`（`get_sidebar_nav` 投影）、仪表盘元信息的 `id`
  （`get_plugin_dashboard_meta`）、已安装插件列表的 `id`，以及插件自身声明的
  API 路径前缀（`get_api()` 拼出的 `/{实例键}/...`）。这些用途都要求该值保持
  实例粒度、不含版本信息，否则路由、仪表盘、配置持久化、API 调用会同时失配。
- `url`：联邦入口地址，由 `PluginManager.get_plugin_remote_entry(plugin_id, dist_path)`
  （`app/runtime/extensions/plugin_manager.py`）拼出，形如
  `/plugin/file/<插件ID小写>/<版本目录>/<dist_path>/remoteEntry.js`。**改动前
  这个方法不接收版本号，只会解析到插件“当前安装版本”的目录**，与调用方到底
  是哪个实例、该实例实际绑定并运行的是哪个版本无关。因此：默认实例通常与
  当前安装版本一致，地址凑巧正确；但被固定（pin）在旧版本的实例、或跟随开关
  关闭因而没有跟着默认实例升级的实例，拿到的地址仍然指向插件当前安装版本，
  与它自己实际运行的 Python 版本不一致——这是本文要修的真正缺陷，比“标识撞名”
  更根本：**地址本身就没有按实例区分版本**。
- `name`：插件展示名，纯文本，不参与任何匹配逻辑。

## 三、后端改动（这次做了什么）

### 3.1 地址按实例绑定的版本解析（修正缺陷，无需前端配合）

`PluginManager.get_plugin_remote_entry` 新增可选参数 `version`：

```python
get_plugin_remote_entry(plugin_id: str, dist_path: str, version: Optional[str] = None) -> str
```

`PluginProjection` 在构造 `remotes()` / `auth_providers()` 的每一条远程入口时，
把该实例运行态插件对象的 `plugin_version` 属性（每个版本各自的源码目录加载出
各自的类，`plugin_version` 就是该实例实际在跑的版本号）传给这个参数，解析出
该版本自己的目录段，不再统一回落到插件当前安装版本。指定版本的目录已被回收
或从未落地时，回落到插件当前安装版本，不报错。

这一项**不需要前端做任何改动**：`url` 字段的格式、字段名都没变，只是同一个
插件的不同实例现在会拿到指向各自版本目录的、彼此不同的地址（此前可能相同）。
没有 `plugin_version` 属性的插件行为不变（走原来的回落路径）。

### 3.2 新增字段：`version` 与 `remote_key`（新增，旧字段不变）

`GET /plugin/remotes`（`PluginRemoteInfo`，`app/schemas/plugin.py`）与认证
提供方的 `remote` 子字段（`AuthProviderRemote`，`app/schemas/user.py`）现在
额外下发两个字段：

```json
{
  "id": "PluginId@second",
  "url": "/plugin/file/pluginid/v2_0_0/dist/assets/remoteEntry.js",
  "name": "插件展示名",
  "version": "2.0.0",
  "remote_key": "PluginId@second#2.0.0"
}
```

- `version`：该远程入口所属实例实际运行的插件版本号；插件未声明
  `plugin_version` 时为 `null`。
- `remote_key`：按版本区分的联邦远程标识，格式为 `"{id}#{version}"`；插件未
  声明 `plugin_version` 时没有版本信息可拼，取值退化为与 `id` 完全相同——这就是
  “单版本时”（或者更准确地说，“没有版本信息可用时”）新字段与旧格式的关系：
  两者相等，旧前端与新前端在这种输入下行为一致。

`id`、`url`、`name` 三个字段的取值语义与格式完全不变。

### 3.3 为什么不直接改造 `id` 字段

评估过“直接把 `id` 改成带版本的格式”这条路，判定为**会必然打断现有前端**，
因此没有这么做：`id` 同时被 `plugin-app.vue` 的路由参数、侧栏导航、仪表盘、
已安装插件列表、以及插件自身 API 路径前缀复用，这些用途全部要求它是**不含
版本信息的实例键**，且这些取用点分布在前端好几个文件、部分还要拼到 REST API
请求路径里（例如 `props.api.get(\`plugin/${pluginId}/history\`)`，后端路由是
按实例键注册的，不认识带版本后缀的路径段）。把 `id` 改成带版本的格式，会让
路由跳转、仪表盘组件加载、插件配置持久化、这类 API 调用同时按新格式去匹配
旧的按实例键注册的后端状态，无法平滑过渡。于是选择新增字段、旧字段原样保留，
把“要不要切换到按版本区分的标识”这个决定交给前端。

## 四、前端需要配合做什么（不改也能工作，改了能彻底消除撞名）

不改前端代码，问题已经缓解到只剩“同一实例升级版本后旧 tab 未刷新”这类正常
的缓存新鲜度问题（`url` 变了，浏览器按新地址重新拉取，不会拿错代码）；两个
**不同实例**绑不同版本这种场景，改动前会稳定撞车，改动后地址已经不同，实测
不会再撞车，因为 `remotesMap` 是按 `id`（已经是实例粒度）注册的，`id` 相同的
只可能是同一个实例反复上报，不存在两个不同实例共用同一个 `id` 的情况——真正
会撞的是“地址算错”而不是“键选错”，3.1 已经修完。

如果要更彻底、面向未来地消除“同一实例先后跑过的两个版本共用同一个联邦注册
键”这类场景（例如同一实例热切换版本、又叠加浏览器端长期不刷新页面的极端
情况），建议：

1. `RemoteModule` 接口（`src/utils/federationLoader.ts`）新增
   `version?: string` 与 `remote_key?: string`。
2. `injectRemoteModule` 注册时优先用 `remote_key`：
   `__federation_method_setRemote(module.remote_key ?? module.id, {...})`；
   `remote_key` 缺失时退化为 `id`，与旧后端兼容。
3. `loadRemoteComponent(id, componentName)` 与
   `loadRemoteAppPageComponent(id, navKey)` 的调用方（`PluginConfigDialog.vue`、
   `PluginDataDialog.vue`、`DashboardElement.vue`、`plugin-app.vue`）目前传入
   的都是各自数据来源给的裸实例键（插件列表的 `id`、仪表盘元信息的 `id`、
   侧栏导航的 `plugin_id`）。`federationLoader.ts` 内部已经会在组件未注册时
   调 `fetchSingleRemoteModule(id)` 从 `/plugin/remotes` 按 `id` 反查完整的
   remote 描述（`fetchSingleRemoteModule` → `fetchRemoteModules` →
   `modules.find(m => m.id === id)`）；同一处改成优先按 `remote_key` 调用
   `__federation_method_getRemote`，即可不再改动这几个组件各自的调用方，只
   改 `federationLoader.ts` 内部这一层。
4. **`id`（以及依赖它的路由参数、API 路径前缀、仪表盘/侧栏/插件列表里的同名
   字段）保持不变**，不要把这些用途换成 `remote_key`——它们的语义是“实例”，
   不是“实例当前跑的这一份联邦构建产物”，混用会打断路由和 API 调用。

新旧字段没有过渡期限、没有开关：`remote_key` 与 `id` 会一直同时下发，前端
什么时候切、要不要切，由前端自行决定，后端不需要感知前端版本。

## 五、后端改不到的残留碰撞面

每个插件自己的联邦构建（`MoviePilot-Plugins` 仓库里插件的 `vite.config.js`，
`federation({ name: 'PluginId', filename: 'remoteEntry.js', exposes: {...} })`）
里的 `name` 会被 `@originjs/vite-plugin-federation` 编译进产物自身，用作该
构建产物内部的 CSS 去重键（形如 `css__{name}__{exposeItemName}`）。如果同一
插件的两个版本在同一页面里同时被加载，且两个版本的构建都用了相同的 `name`
（插件作者通常会让它等于插件 ID，两个版本大概率相同），后加载的版本可能因为
DOM 里已经有同名的样式标签而跳过样式注入，出现“组件能加载但样式不对”的问题。
这发生在插件自己的构建产物内部，MoviePilot 后端和 MoviePilot-Frontend 都无法
从外部修正；如果实际遇到，只能建议插件作者按版本变化 `federation()` 的
`name` 配置（例如拼上版本号）。这不是本次改动要解决的范围，此处仅记录以便
排查类似现象时不必再重新定位到这一层。
