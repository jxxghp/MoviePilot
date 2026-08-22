# 插件原生级扩展能力验证

## 本仓现状：这条路线没有代码

这份文档是一次一次性验证的记录，方向已被否决（判据见
`docs/plugin-extension-architecture.md` §2.3）。**验证用到的东西本仓一件都没有**：

| 验证时用到的东西 | 本仓状态 |
|---|---|
| 插件包 `app/plugins/rclonestorageplugin/` | 不存在。`.gitignore` 只放行 `githubsso`、`p123disk`、`servicehealth` 三个参考实现 |
| 测试 `tests/test_plugin_native_capability.py` | 不存在 |
| `plugin_module` capability kind | 不存在。`app/runtime/extensions/lifecycle/host_module_adapter.py` 只登记 `HOST_MODULE_KIND = "host_module"` |
| `build_host_module_registry(extra_roots=...)` | 不存在。该函数不接受任何参数，声明根写死为 `app/modules` |
| `configure_plugin_capability_roots()` / `ModuleManager._discover_plugin_capability_roots()` | 不存在 |

因此**插件目前不能作为一等模块参与分发**。`CapabilityRegistry.discover()` 本身仍是通用的
（接受多个声明根、接受任意 kind 集合），缺的是把插件根接进去的那一层，以及本文第 5 条记下的
隔离缺口。下文是那次验证的结论，不是对当前行为的描述。

## 验证的问题

插件提供的模块能否像 `app/modules/` 下的内置模块一样，被能力注册表发现、被
Capability Runtime 装载、参与四级分发（广播/多播/单播/管道）。样本用
`app/modules/rclone/` 复制出一个插件形态的存储扩展来验证，`app/modules/rclone/`
本身未做任何改动。

## 插件包

```
app/plugins/rclonestorageplugin/v1_0_0/
├── capability.toml   # kind = plugin_module
└── __init__.py       # RcloneStoragePlugin（插件主类）+ RcloneStorageModule（存储模块）
```

`capability.toml` 与内置模块的格式完全一致，只是 `kind` 不同：

```toml
schema_version = 1
id = "RcloneStorageModule"
kind = "plugin_module"
entrypoint = "app.plugins.rclonestorageplugin.v1_0_0:RcloneStorageModule"
```

`RcloneStorageModule` 直接复用内置基类 `app.modules._base._StorageModuleBase`，
声明 `storage_class = RclonePluginStorage`；生命周期、能力方法转发、`storage_backend_registry`
登记全部走内置基类既有实现，插件没有另写一套。`RcloneStoragePlugin` 是插件主类
（继承 `app.plugins._PluginBase`），只负责开关和配置页面，不承载存储能力。

**存储标识如何避开冲突**：`app/modules/_base/storage.py` 里 `storage_backend_identity()`
读取的是后端类的 `schema` 属性（`getattr(schema, "value", schema)`），不要求这个
值是 `StorageSchema` 枚举成员，任何满足 `FileURI.is_storage_scheme()` 校验的字符串
都可以（字母开头、长度≥2）。内置 `Rclone.schema = StorageSchema.Rclone`（值
`"rclone"`）；插件后端 `RclonePluginStorage.schema = "rclone_plugin"`，是一个普通
字符串，不登记进 `StorageSchema` 枚举。两者在 `storage_backend_registry` 里以不同
键各自登记，验证中确认过二者能同时存在（`storage_ids()` 同时含 `"rclone"` 与
`"rclone_plugin"`）。

**存储后端本身没有照抄 `rclone.py` 的进程调用实现**，而是用一个内存字典模拟的
文件树（`RclonePluginStorage`），实现了 `StorageBase` 的全部抽象方法（`list`/
`create_folder`/`get_folder`/`get_item`/`delete`/`rename`/`download`/`upload`/`detail`/
`copy`/`move`/`usage`，`link`/`softlink` 返回 `False` 表示不支持）。这足以证明「插件
能提供一个存储后端」，不需要也不应该证明「插件能替换 rclone」。

## 五条命题的验证结果

### 1. capability.toml 被发现，kind 为 plugin_module，entrypoint 可物化 —— 成立

`CapabilityRegistry.discover((插件版本目录,), kinds={"plugin_module"}, ...)` 能独立
发现这份声明；`spec.kind == "plugin_module"`；`entrypoint` 经与内置模块共用的
`HostModuleAdapter.materialize()` 物化出真实类对象，`issubclass(实现类,
_StorageModuleBase)` 成立。

### 2. 内置模块不受影响，仍然全部被发现，数量不变 —— 成立

用生产入口 `build_host_module_registry(extra_roots=(插件版本目录,))` 验证：加入
插件根前后，`kind == "host_module"` 的声明集合（按 id 比对）完全一致。数量按
`len(build_host_module_registry().list_specs())` 动态取得，验证里没有硬编码，工单里提到的
46 与当时实测的 44 有出入但不影响命题本身是否成立。本仓当前的宿主模块数是 **46**。

### 3. 插件模块进入运行态，出现在 ModuleManager 的运行模块视图里，与内置模块同一视图 —— 成立

验证时 `ModuleManager` 单例的构造路径是：

```python
registry = build_host_module_registry(self._discover_plugin_capability_roots())
adapter = HostModuleAdapter()
self._runtime = CapabilityRuntime(
    registry,
    adapters={HOST_MODULE_KIND: adapter, PLUGIN_MODULE_KIND: adapter},
    ...
)
```

`_discover_plugin_capability_roots()` 从模块级可注入的 `_plugin_capability_roots`
提供者读取扩展声明根（通过 `configure_plugin_capability_roots(provider)` 注入），
读取失败或目录不存在时按「无扩展」处理，不影响宿主模块装载——这三种情形分别有验证覆盖。

真正装载插件模块的那条验证构造了一个真实的 `ModuleManager()` 单例：注册表通过真实
的 `CapabilityRegistry.discover()` 构建，同时含一个安全的合成宿主模块（避免验证
里真的激活全部内置模块触发外部连接）与真实的插件模块声明，`ModuleManager`
其余的装载、投影、单例生命周期逻辑完全不打桩。结果：

- `manager.get_running_module("RcloneStorageModule")` 返回非空运行实例；
- 该实例类型就是插件源码里定义的 `RcloneStorageModule`；
- 存储后端标识 `"rclone_plugin"` 出现在 `storage_backend_registry.storage_ids()` 里。

### 4. 插件模块参与能力索引，声明的存储能力方法能通过能力索引被查到，与内置模块同级 —— 成立

同一个真实 `ModuleManager()` 单例上：`manager.providers_for("list_files")` 同时
包含合成宿主模块实例与插件模块实例；`manager.get_module_capabilities("RcloneStorageModule")`
含 `"list_files"`。进一步通过索引取到的插件实例发起真实调用
（`create_folder` 后 `list_files` 能读到刚创建的目录），证明索引里挂的是可真实
分发到的方法，不是只有同名却不可用的占位。

### 5. 坏插件清单不击穿内核 —— 不成立（发现一个真实缺口）

`_discover_plugin_capability_roots()` 只兜底两种情形：provider 本身抛错、返回的
目录不存在——这两种都会被过滤/吞掉，不连累宿主模块。但它**不处理第三种情形**：
目录存在、但目录下的 `capability.toml` 内容不合法。

`build_host_module_registry(extra_roots=(坏插件版本目录,))` 在这种情形下会直接抛出
`CapabilityManifestError`，不吞不跳过。根因在更底层，且**这一层今天仍然如此**：
`CapabilityRegistry.discover()` 对多根的语义是「把全部根的 `capability.toml` 一次性
rglob 出来逐个解析，任何一个解析失败就整批构造失败」，不区分是哪个根出的问题。如果
把「已装插件当前生效版本目录」的列表整体作为多根传给一次 `discover()` 调用（最自然的
接法），一个第三方插件写错 `capability.toml`，会连同全部宿主模块一起装不起来——这是比
「单个插件用不了」严重得多的故障半径。

验证同时确认了一个可行的规避方式：按插件各自的根**分别**调用
`CapabilityRegistry.discover()` 并各自 `try/except`，一个插件的坏声明确实只影响
它自己，其余插件与宿主模块的发现互不干扰。这说明问题不在 `CapabilityRegistry`
本身的能力上限，而在于「要不要、以及在哪一层做按插件根的隔离」这个调用方式的选择。

## 要走通这条路线还缺什么

以下四条按验证当时的口径记录。由于内核侧接口已不在本仓，第 1–3 条的前置条件是先把
`plugin_module` kind 与多根发现重新做出来。

1. **坏插件清单的隔离**（命题 5）：需要在 `build_host_module_registry` 或其调用方
   （`ModuleManager.__init__`）里按插件根分别 `discover()` 并捕获单根失败，而不是
   把全部插件根塞进一次 `discover()` 调用。风险等级不对称：宿主模块由代码审查兜底，
   插件不能假定同等质量，一个第三方插件的笔误不该让整个模块子系统装不起来。

2. **插件声明根的生产 provider**：注入点本身在验证中工作正常，但没有任何
   `app/startup/**` 代码调用它——即使一个插件按本文档的方式声明了 `capability.toml`，
   真实跑起来的 MoviePilot 里 `ModuleManager()` 单例也发现不到它，因为没有人告诉它
   插件的版本目录在哪里。这一步需要在启动组合根里，找到「已装插件 + 当前生效版本」的
   来源（`app/runtime/extensions/lifecycle/layout.py` 的 `plugin_version_dirs`/
   `resolve_plugin_version_dir` 已有版本解析逻辑），包成一个不反查数据库的 provider。
   运行时层禁止反向依赖 `app/db`，该 provider 必须放在启动组合根而不是 `app/runtime`
   内部，超出了「内核开门」本身的范围。

3. **插件卸载 / 版本切换时的能力反注册**：`ModuleManager.stop()`/`shutdown()` 会
   正确调用到插件模块的 `stop()`（验证过，`storage_backend_registry` 在
   `runtime.shutdown()` 后清空），但插件「卸载」或「切换版本」这类场景
   （`app/runtime/extensions/plugin_manager.py` 已有一整套版本绑定与回收机制）
   与 `ModuleManager` 的能力注册表之间如何联动，本次验证完全没有触碰，因为这依赖
   上一条缺失的 provider 接线。

4. **多个插件同时声明同一个 `capability_id` 的冲突判定**：`CapabilityRegistry.discover()`
   已有「id 重复直接报错」的判据，在单次 `discover()` 调用范围内验证过。这个判据是否
   也需要按插件根隔离（避免一个插件的 id 冲突连累其它插件），跟缺口 1 是同一类问题，
   没有单独验证。

## 命题结果汇总

| # | 命题 | 结果 |
|---|------|------|
| 1 | capability.toml 被发现且可物化 | 成立 |
| 2 | 内置模块不受影响 | 成立 |
| 3 | 插件模块进入运行态，与内置模块同一视图 | 成立（真实 ModuleManager 单例） |
| 4 | 插件模块参与能力索引，与内置模块同级 | 成立（真实 ModuleManager 单例） |
| 5 | 坏插件清单不击穿内核 | 不成立，已定位具体缺口 |

以上五行是那次验证的结果，不是本仓当前具备的能力——见文首「本仓现状」。
