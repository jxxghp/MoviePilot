# MoviePilot 多媒体、多数据源自动分类体系设计

- 状态：Draft
- 日期：2026-09-02
- 适用版本：MoviePilot V3
- 涉及仓库：`MoviePilot`、`MoviePilot-Frontend`，后续插件能力文档涉及 `MoviePilot-Plugins`

## 1. 背景

MoviePilot 当前自动分类能力已经有后端 API 和前端编辑弹窗，但实现本质上仍是
TheMovieDb 模块内部的专用能力：

- `app/modules/themoviedb/category.py` 直接读取和写入 `config/category.yaml`。
- 分类配置只有 `movie`、`tv` 两个固定根节点。
- 分类条件直接对应 TMDB 原始字段，例如 `genre_ids`、`original_language`、
  `production_countries`、`origin_country`。
- `app/modules/themoviedb/__init__.py` 在构造 TMDB `MediaInfo` 时直接计算并写入
  `MediaInfo.category`。
- `app/chain/base.py` 通过模块方法 `media_category`、`load_category_config`、
  `save_category_config` 间接访问 TMDB 分类器。
- `MoviePilot-Frontend/src/components/dialog/CategoryEditDialog.vue` 写死电影、电视剧、
  TMDB Genre ID、语种和国家选项。

这导致以下问题：

1. 使用豆瓣、Bangumi、AniList、IMDb、TVDB 或插件数据源识别时，无法得到与 TMDB
   等价的自动目录分类。
2. 音乐虽然已有 MusicBrainz、TheAudioDB、豆瓣音乐以及本地音频标签能力，但没有独立的
   自动分类策略。
3. 来源原始字段与分类规则直接绑定，来源接口变化会使用户规则失效。
4. 分类逻辑在来源模块内部执行，缓存命中、同步/异步识别、插件识别等路径容易出现行为差异。
5. 当前 `category` 字段存在语义冲突：影视使用它表示媒体库分类，音乐使用它表示
   `Album / Live`、流派等来源元数据；整理历史又统一把该字段写成目录分类。
6. 分类名称同时充当稳定身份和目录路径，重命名分类会影响目录设置、订阅和历史记录。
7. 当前 UI 只能编辑固定条件，不能表达组合条件、来源范围、兜底规则、规则解释和发布前预览。

本设计将分类能力从 TMDB 模块中拆出，建立一套来源无关、媒体类型可扩展、可解释、可迁移、
可由前端完整配置的统一分类体系。

## 2. 目标

### 2.1 功能目标

1. 支持电影、电视剧、音乐，后续新增媒体类型时不需要重写规则引擎。
2. 支持所有注册媒体数据源，包括内置来源和插件来源。
3. 同一条基于标准字段的规则可以跨来源工作，不要求用户理解来源原始 JSON。
4. 允许规则按媒体类型、数据源、语言、国家、年份、类型、标签、音乐实体和专辑属性等条件匹配。
5. 支持 `AND`、`OR`、`NOT`、范围、包含、缺失值等明确的组合语义。
6. 每个媒体只产生一个主目录分类，同时允许附加多个非目录标签。
7. 支持多级目录分类，并使用稳定分类 ID，目录名称可以安全重命名。
8. 前端支持新增、复制、排序、启停、校验、测试、影响预览和回滚。
9. 分类结果可解释：能够回答“命中了哪条规则”“哪些条件通过或失败”“使用了哪些事实”。
10. 保留现有 `category.yaml` 用户配置，并提供无损迁移与旧 API 兼容期。

### 2.2 工程目标

1. 分类规则求值必须是纯函数，不执行网络、文件或数据库 I/O。
2. 分类器不直接依赖 TMDB、豆瓣、MusicBrainz 等具体模块。
3. 同步和异步识别路径使用同一个分类结果收口点。
4. 媒体身份始终保留原始 `media_source + media_id`，分类不得隐式转换主身份。
5. 识别缓存命中后仍按当前策略重新分类，避免策略更新后返回旧分类。
6. 整理任务进入持久计划后冻结分类结果和策略版本，运行中的任务不受后续策略修改影响。
7. 用户配置通过 `SystemConfigKey` 管理，不继续由某个来源模块拥有配置文件。

### 2.3 非目标

1. 本设计不替代搜索、订阅使用的资源过滤规则和优先级规则。
2. 默认不为了分类而调用所有外部数据源，避免额外延迟、配额消耗和不稳定性。
3. 分类器不允许 UI 直接执行任意 Python 表达式、Jinja、JSONPath 或未受控正则。
4. 来源原始响应不作为长期规则协议；原始字段只能先投影为已登记的分类事实。

## 3. 核心设计原则

### 3.1 分类晚于来源投影，早于目录选择

标准顺序为：

```text
文件名/身份输入
      |
      v
数据源识别或插件识别
      |
      v
统一 MediaInfo / MusicInfo 投影
      |
      v
构造 ClassificationFacts
      |
      v
执行自动分类策略
      |
      v
应用人工覆盖并冻结有效分类
      |
      v
目录选择、下载历史、订阅、整理计划
```

分类不再由任何具体来源在构造媒体对象时顺便完成。

### 3.2 多数据源不等于强制转换为 TMDB

当媒体由豆瓣、Bangumi、AniList、MusicBrainz 或插件来源识别时：

- 保留该来源的 `media_source + media_id`。
- 只从该来源已经投影出的标准事实进行分类。
- 不为得到分类而把身份替换成 TMDB ID。
- 可选跨源补充只填充缺失事实，不改变主身份，也不覆盖主来源明确提供的值。

### 3.3 目录分类与来源分类分离

统一采用以下术语：

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `metadata_category` | 数据源提供的描述性分类 | `Album / Live`、`Rock` |
| `library_category` | 自动分类或人工覆盖后的媒体类型内相对目录分类 | `现场专辑` |
| `classification_labels` | 不参与目录选择的附加标签 | `华语`、`高解析` |
| `category` | 兼容字段，过渡期只映射到 `library_category` | `现场专辑` |

音乐来源当前写入 `category` 的专辑类型、流派信息必须迁移到 `metadata_category`。前端音乐详情和卡片
展示 `metadata_category`；目录、订阅和整理历史只消费 `library_category`。

### 3.4 稳定 ID 与可变路径分离

分类使用不可变 `category_id` 作为引用，使用可变 `path` 作为目录快照：

```json
{
  "id": "music.live",
  "media_type": "音乐",
  "name": "现场专辑",
  "path": ["现场专辑"]
}
```

`path` 始终相对于媒体类型根目录；是否增加 `电影`、`电视剧`、`音乐` 一级目录仍由目录配置中的
按类型分类开关决定。分类改名只改变 `name` 或 `path`，目录配置、订阅和规则仍引用 `music.live`。

## 4. 总体架构

```text
                           +------------------------------+
                           | MoviePilot-Frontend          |
                           | 策略编辑器 / 预览 / 影响分析 |
                           +---------------+--------------+
                                           |
                                           v
+---------------------+       +------------+-------------+
| Media Source Catalog|------>| Classification API       |
| 内置来源 + 插件来源 |       | policy/fields/preview    |
+----------+----------+       +------------+-------------+
           |                               |
           v                               v
+----------+-------------------------------+-------------+
| Application: classification                            |
| 策略读取、版本检查、人工覆盖、结果发布、配置迁移       |
+-----------------------------+--------------------------+
                              |
                              v
+-----------------------------+--------------------------+
| Domain: classification                                   |
| ClassificationFacts + Rule AST + 纯求值器 + 命中解释    |
+-----------------------------+--------------------------+
                              ^
                              |
+-----------------------------+--------------------------+
| MediaInfo / MusicInfo / MetaBase / MetaMusic             |
| 来源投影后的标准字段 + 受控扩展事实                      |
+----------------------------------------------------------+
```

### 4.1 后端模块归属

建议新增同名包 `app/domain/classification/`：

| 文件 | 责任 |
| --- | --- |
| `model.py` | 分类、策略、条件树、事实、结果和解释的不可变领域模型 |
| `facts.py` | 从标准媒体对象构造来源无关事实，不读取运行时配置 |
| `evaluator.py` | 纯规则求值、顺序控制和命中解释 |
| `validation.py` | 字段、操作符、路径和不可达规则的纯校验 |

建议新增 `app/application/classification/`：

| 文件 | 责任 |
| --- | --- |
| `service.py` | 读取当前策略、分类媒体、解析人工覆盖、返回有效结果 |
| `configuration.py` | 策略发布、版本冲突检查、历史快照和 legacy 迁移 |
| `catalog.py` | 合并内置字段目录与插件来源能力声明 |
| `contract.py` | Application 使用的配置端口和公开 DTO |

建议新增 `app/startup/composition/classification.py`，负责：

- 使用 `SystemConfigService` 装配策略仓储。
- 构造唯一的 `MediaClassificationService`。
- 注入媒体链和 API runtime。
- 在启动时执行一次 legacy 配置迁移。

`app/chain/` 只负责在识别和整理工作流中调用已注入的分类服务，不读取配置文件，不实现条件比较。

### 4.2 前端模块归属

建议用以下组件替换当前单体 `CategoryEditDialog.vue`：

| 文件 | 责任 |
| --- | --- |
| `src/components/classification/ClassificationPolicyDialog.vue` | 编辑器外壳、加载、保存和发布 |
| `ClassificationCategoryList.vue` | 分类树、排序、启停和路径编辑 |
| `ClassificationRuleList.vue` | 规则列表、复制、拖拽和摘要 |
| `ClassificationConditionBuilder.vue` | 递归条件组编辑器 |
| `ClassificationRuleInspector.vue` | 当前规则详细配置 |
| `ClassificationPreviewPanel.vue` | 单条测试、事实查看和命中解释 |
| `ClassificationImpactDialog.vue` | 发布前批量影响分析 |
| `src/composables/useMediaClassification.ts` | API、草稿、脏状态和能力目录缓存 |

## 5. 领域模型

### 5.1 分类策略

```json
{
  "schema_version": 2,
  "revision": 12,
  "mode": "first_match",
  "categories": [],
  "rules": [],
  "fallbacks": {},
  "source_fallbacks": {},
  "field_aliases": {},
  "updated_at": "2026-09-02T12:00:00+08:00"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 配置结构版本，用于后续迁移 |
| `revision` | 每次成功发布递增，用于并发修改检测和任务快照 |
| `mode` | 主分类规则首版固定为 `first_match`，保留后续策略扩展位置 |
| `categories` | 稳定分类定义 |
| `rules` | 全局有序的主分类规则和附加标签规则 |
| `fallbacks` | 每种媒体类型的兜底分类 ID |
| `source_fallbacks` | 可选的数据源级媒体类型兜底；优先于通用兜底，用于兼容来源特有的历史语义 |
| `field_aliases` | 可选值别名，例如音乐流派同义词，不改变字段 ID |

### 5.2 分类定义

```json
{
  "id": "tv.anime.jp",
  "media_type": "电视剧",
  "name": "日番",
  "path": ["动漫", "日番"],
  "enabled": true,
  "labels": ["动漫", "日本"]
}
```

约束：

- `id` 使用稳定小写标识，允许小写字母、数字、点、下划线和连字符。
- `path` 使用路径段数组，不接受用户直接输入带 `/` 或 `\\` 的完整路径。
- 单段禁止空值、`.`、`..`、控制字符和平台非法文件名。
- 建议最大 4 级、单段 64 字符、总长度 240 字符。
- 同一媒体类型下目录路径必须唯一。
- 删除已被目录、订阅或规则引用的分类前必须先解除引用；普通改名不改变 ID。

### 5.3 规则定义

```json
{
  "id": "rule.tv.anime.jp",
  "name": "日本动画",
  "kind": "category",
  "enabled": true,
  "priority": 100,
  "media_types": ["电视剧"],
  "sources": [],
  "when": {
    "all": [
      {
        "field": "media.genre_keys",
        "operator": "contains_any",
        "value": ["animation"]
      },
      {
        "field": "media.countries",
        "operator": "contains_any",
        "value": ["JP"]
      }
    ]
  },
  "target": {
    "category_id": "tv.anime.jp",
    "labels": ["动漫"]
  }
}
```

规则顺序由列表位置决定，`priority` 是服务端保存后的稳定投影。`kind` 分为：

- `category`：主目录分类规则，必须指定 `category_id`。
- `label`：附加标签规则，只能输出 `labels`，不得改变主目录分类。

首版采用以下确定性语义：

1. 跳过禁用规则。
2. 先检查媒体类型。
3. `sources` 为空时允许所有来源；非空时只允许列出的来源。
4. 递归求值 `when`。
5. `category` 规则采用首条命中，确定主分类后忽略后续 `category` 规则。
6. 所有命中的 `label` 规则累积标签并稳定去重；命中的主分类规则也可附带标签。
7. 没有主分类规则命中时使用该媒体类型的 `fallbacks`。

### 5.4 条件树

条件树只有两种节点：条件组和叶子条件。

```json
{
  "any": [
    {"field": "media.language", "operator": "in", "value": ["zh", "yue"]},
    {
      "all": [
        {"field": "media.countries", "operator": "contains_any", "value": ["CN", "HK", "TW"]},
        {"field": "media.year", "operator": "between", "value": [2000, 2099]}
      ]
    }
  ]
}
```

支持的组：

- `all`：全部子项为真。
- `any`：任一子项为真。
- `not`：仅包含一个子项并取反。

操作符由字段类型决定：

| 类型 | 操作符 |
| --- | --- |
| 字符串 | `equals`、`not_equals`、`in`、`not_in`、`contains`、`starts_with`、`ends_with` |
| 数字/年份 | `equals`、`not_equals`、`gt`、`gte`、`lt`、`lte`、`between` |
| 字符串列表 | `contains_any`、`contains_all`、`contains_none` |
| 布尔 | `is_true`、`is_false` |
| 任意字段 | `exists`、`not_exists` |

缺失值规则必须固定：

- 除 `not_exists` 外，字段缺失一律判定为不匹配。
- `not_in`、`not_equals`、`contains_none` 不得把缺失值当成命中。
- 用户要匹配缺失值时必须显式使用 `not_exists`。

这可避免来源字段不足时意外落入排除型规则。

### 5.5 分类事实

规则不直接读取 `tmdb_info`、`douban_info`、`bangumi_info`、`anilist_info`、
`MusicInfo.raw_data`。求值输入统一为 `ClassificationFacts`：

```json
{
  "identity": {
    "media_source": "musicbrainz",
    "media_id": "..."
  },
  "media": {
    "type": "音乐",
    "title": "示例专辑",
    "year": 2024,
    "language": null,
    "countries": ["JP"],
    "genre_keys": ["rock"],
    "genre_names": ["Rock"],
    "adult": false
  },
  "music": {
    "entity_type": "album",
    "album_type": "Album",
    "secondary_types": ["Live"],
    "tags": ["j-rock"],
    "artists": ["示例艺术家"]
  },
  "extensions": {}
}
```

建议的标准字段目录：

| 字段 ID | 类型 | 适用类型 | 说明 |
| --- | --- | --- | --- |
| `identity.media_source` | string | 全部 | 当前主身份来源 |
| `media.type` | enum | 全部 | 电影、电视剧、音乐 |
| `media.year` | integer | 全部 | 标准发行或首播年份 |
| `media.language` | string | 全部 | ISO 639 标准语言代码 |
| `media.countries` | list | 全部 | ISO 3166 国家/地区代码 |
| `media.genre_keys` | list | 全部 | MoviePilot 规范化类型键 |
| `media.genre_names` | list | 全部 | 来源投影后的类型名称 |
| `media.adult` | boolean | 影视 | 成人内容标记 |
| `media.runtime` | integer | 影视 | 分钟 |
| `media.content_rating` | string | 影视 | 内容分级 |
| `media.companies` | list | 影视 | 出品公司或工作室名称 |
| `media.networks` | list | 电视剧 | 电视台或平台名称 |
| `music.entity_type` | enum | 音乐 | recording、album、artist |
| `music.album_type` | string | 音乐 | Album、EP、Single 等 |
| `music.secondary_types` | list | 音乐 | Live、Compilation、Soundtrack 等 |
| `music.genres` | list | 音乐 | 标准化音乐流派名称 |
| `music.tags` | list | 音乐 | 来源标签 |
| `music.artist_country` | string | 音乐 | 艺术家国家/地区 |
| `music.release_status` | string | 音乐 | Official、Bootleg 等 |

`media.genre_keys` 用于跨来源通用规则，值采用 MoviePilot 稳定词表，例如
`animation`、`documentary`、`reality`、`talk`、`kids`、`music`。来源投影负责把 TMDB 数字 ID、
豆瓣类型名、Bangumi 标签、AniList Genre 等转换为这些规范键；无法规范化的值仍保留在
`genre_names` 或音乐 `genres/tags` 中。

### 5.6 插件扩展事实

插件媒体来源可以声明额外分类字段，但必须满足：

1. 字段只能位于 `extensions.<media_source>.<field>` 命名空间。
2. 字段描述包括显示名、值类型、允许操作符、适用媒体类型和可选枚举值。
3. 插件返回的事实必须是 JSON 标量或标量列表。
4. 插件不能覆盖 `identity.*`、`media.*`、`music.*` 标准字段。
5. 插件未加载或字段缺失时，相关条件按缺失值语义处理。

建议扩展 `MediaSourceInfo`：

```json
{
  "name": "Example Source",
  "media_source": "example.source",
  "media_types": ["电影", "电视剧"],
  "classification_fields": [
    {
      "id": "extensions.example.source.region_group",
      "label": "来源地区组",
      "value_type": "string",
      "operators": ["equals", "in"]
    }
  ]
}
```

插件识别结果通过受控的 `classification_facts` 字段提供对应值。键必须使用完整
`extensions.<media_source>.<field>` 字段 ID；宿主按结果自身的 `media_source`、媒体类型、当前启用插件注册表和
字段值类型验证后，转换为分类器内部的来源局部事实。无效字段按稳定诊断码记录并忽略，不得改变
`media_source + media_id`，也不得导致媒体识别失败。

插件通过稳定入口 `app.sdk.classification` 检测
`MEDIA_SOURCE_CLASSIFICATION_PROTOCOL_VERSION >= 1`，并可从该入口导入 `MediaSourceInfo`、
`ClassificationFieldDefinition` 和 `ClassificationFactValue`。旧宿主或未声明字段的旧插件保持原有
`get_media_source()` 与识别返回值；不会隐式获得扩展事实写入权限。插件禁用、停止、卸载、重载或启动失败时，
运行时立即撤销对应运行实例拥有的字段注册，已保存规则保留但重新校验为字段不可用。

## 6. 多数据源能力模型

### 6.1 来源能力目录

后端必须成为字段能力的唯一事实来源，前端不再硬编码 TMDB Genre、国家和语言字段。

`GET /api/v1/media/classification/fields` 返回：

```json
{
  "fields": [
    {
      "id": "media.countries",
      "label": "国家/地区",
      "value_type": "string_list",
      "operators": ["contains_any", "contains_all", "contains_none", "exists", "not_exists"],
      "media_types": ["电影", "电视剧", "音乐"],
      "options": [],
      "source_support": {
        "themoviedb": "native",
        "douban": "derived",
        "anilist": "native",
        "musicbrainz": "partial"
      }
    }
  ]
}
```

支持等级：

| 值 | 含义 |
| --- | --- |
| `native` | 来源直接提供标准值 |
| `derived` | 可从来源其它字段稳定推导 |
| `partial` | 只有部分对象或详情级响应提供 |
| `extension` | 插件声明的扩展字段 |
| `unavailable` | 来源无法提供 |

UI 根据用户选择的媒体类型和来源过滤字段，并显示覆盖提示。规则仍允许保存 `partial` 字段，但必须提示
“字段缺失时本规则不会命中”。

### 6.2 可选跨源补充

首版默认使用 `primary_only`：只使用当前识别结果和本地解析事实。

后续可增加 `enrich_missing`：

- 只请求当前策略实际引用且当前缺失的标准事实。
- 使用独立可选模块方法 `get_media_classification_facts`。
- 通过外部 ID 或明确映射查找同一媒体，不改变主 `media_source + media_id`。
- 单来源失败隔离，设置总超时、并发上限和 TTL 缓存。
- 结果记录每个事实的提供来源，预览中可查看。
- 整理主流程在补充失败时继续使用已有事实，不把分类补充变成整理硬依赖。

## 7. 分类结果与覆盖规则

### 7.1 分类结果

```json
{
  "recommended": {
    "category_id": "music.live",
    "category_path": ["现场专辑"],
    "rule_id": "rule.music.live"
  },
  "effective": {
    "category_id": "music.live",
    "category_path": ["现场专辑"],
    "source": "automatic"
  },
  "labels": ["现场"],
  "policy_revision": 12,
  "state": "complete"
}
```

`state` 可为：

- `complete`：使用详情级事实完成求值。
- `partial`：存在规则所需字段缺失，但仍得到其它规则或兜底结果。
- `not_evaluated`：列表摘要等路径没有执行分类。
- `invalid_policy`：策略不可用，回退兼容行为并记录错误。

### 7.2 有效分类优先级

最终 `effective` 按以下优先级解析：

1. 当前请求明确指定的 `category_id`。
2. 订阅、下载历史或人工整理持久化的分类覆盖。
3. 用户显式选定目录时，该目录绑定的固定分类。
4. 当前策略自动推荐分类。
5. 当前数据源声明的媒体类型兜底分类。
6. 媒体类型通用兜底分类。

来源 `metadata_category` 永远不自动升级为 `library_category`。用户确实希望按 `Live`、`Album`、`Rock`
分类时，应通过显式规则引用 `music.secondary_types`、`music.album_type`、`music.genres`。

## 8. 执行与生命周期

### 8.1 识别结果收口

在 `MediaChain` 中新增统一的结果收口方法，例如 `_finalize_recognition_result()`：

1. 校验并规范化 `media_source + media_id`。
2. 完成来源到 `MediaInfo` / `MusicInfo` 的标准投影。
3. 构造 `ClassificationFacts`。
4. 执行当前策略并写入 `library_category` 和 `classification`。
5. 保留 `metadata_category`。
6. 返回最终对象。

所有同步、异步、按标题、按 ID、缓存命中、插件识别路径必须经过该方法。分类器本身纯同步，异步路径不
维护第二份实现。

### 8.2 缓存语义

- 来源识别缓存只缓存来源投影结果，不把自动分类结果作为长期真值。
- 每次从识别缓存恢复对象后，使用当前策略重新分类。
- 如需要分类结果缓存，键必须包含 `policy_revision` 和分类事实摘要。
- 修改策略无需清空 TMDB、MusicBrainz 等识别缓存。

### 8.3 整理任务冻结

整理计划创建时写入：

- `category_id`
- `library_category` 路径快照
- `classification_rule_id`
- `classification_policy_revision`
- 覆盖来源 `automatic/manual/subscription/directory`

任务进入 durable transfer 计划后不得重新读取当前分类策略。重启恢复继续使用计划中的快照，避免策略修改
导致同一个任务目标目录变化。

### 8.4 订阅与历史

- `Subscribe.media_category` 兼容字段继续保存路径快照。
- 新增稳定的 `media_category_id`，人工选择时优先保存 ID。
- 自动分类不需要在订阅创建时永久锁死；没有人工覆盖的订阅在每次取得完整媒体信息时使用当前策略。
- 下载历史、整理历史和订阅历史保存实际执行时的分类 ID、路径和策略版本。
- 分类解释可保存在现有结构化 `note` 中，避免为完整条件明细增加大量列；常用查询字段使用正式列。

## 9. 配置持久化与版本

### 9.1 存储方式

新增 `SystemConfigKey.MediaClassificationPolicy`，值为版本化策略状态包：

```json
{
  "active": {"schema_version": 2, "revision": 12, "categories": [], "rules": []},
  "history": [
    {"schema_version": 2, "revision": 11, "categories": [], "rules": []}
  ]
}
```

API 常规读取只返回 `active`，历史接口按需读取 `history`。选择该存储方式的理由：

- 分类策略是管理员可编辑的运行时业务配置。
- 不需要为分类和规则各建一张高频查询表。
- 策略可以作为一个整体原子校验、替换和发布。
- 与现有目录、过滤规则等配置管理方式一致。

同一个配置值中保留最近 10 个已发布快照，避免活跃策略和历史分别写入两个 key 造成非原子状态。

### 9.2 发布并发控制

更新请求必须携带客户端读取到的 `revision`：

1. Application 定义原子 `compare_and_set(expected_revision, state)` 配置端口。
2. DB Adapter 在同一事务中锁定对应 SystemConfig 行、重新读取当前 revision 并完成整体替换。
3. revision 不一致时返回 `409 Conflict`，不覆盖他人修改。
4. 校验通过后 revision 加一，旧版本进入有界历史。
5. 提交成功后刷新进程内不可变策略引用；进程锁只用于减少同进程竞争，不能替代数据库原子检查。

### 9.3 回滚

回滚不是覆盖旧 revision，而是选择历史快照内容并发布为新 revision。这样审计、运行中整理任务和缓存键
都保持单调版本语义。

## 10. API 设计

### 10.1 新 API

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/media/classification/policy` | 登录用户 | 读取当前策略和 revision |
| `PUT` | `/api/v1/media/classification/policy` | 超级管理员 | 校验并发布策略 |
| `GET` | `/api/v1/media/classification/fields` | 登录用户 | 获取字段、操作符和来源覆盖 |
| `POST` | `/api/v1/media/classification/validate` | 超级管理员 | 仅校验草稿，不保存 |
| `POST` | `/api/v1/media/classification/preview` | 登录用户 | 对给定媒体或事实执行只读预览 |
| `POST` | `/api/v1/media/classification/impact` | 超级管理员 | 对近期历史样本比较旧/新策略 |
| `GET` | `/api/v1/media/classification/history` | 超级管理员 | 查询可回滚版本 |
| `POST` | `/api/v1/media/classification/rollback/{revision}` | 超级管理员 | 以历史内容发布新版本 |

字段目录响应同时返回服务端规则限制。枚举型字段的 `options` 使用稳定的
`{value, label}` 对象，`allow_custom_values` 明确表示前端是否允许目录外输入；前端不得根据字段名硬编码
输入控件或限制值。预览请求使用带 `kind` 判别字段的输入联合体，首期支持
`{"kind": "facts", "facts": {...}}`，后续身份查询和历史选择可在不破坏现有客户端的前提下增加分支。

发布、回滚和影响分析都携带 `expected_revision`。revision 不一致时返回标准 `Response` 包装的
`409`，`data` 为 `{expected_revision, current_revision}`；领域校验失败返回标准 `Response` 包装的
`422`，`data` 为完整 `ClassificationValidationResult`。客户端收到任一响应都必须保留本地草稿。

### 10.2 预览响应

```json
{
  "facts": {},
  "result": {},
  "trace": [
    {
      "rule_id": "rule.music.live",
      "matched": true,
      "conditions": [
        {
          "path": "when.all[0]",
          "field": "music.secondary_types",
          "operator": "contains_any",
          "expected": ["Live"],
          "actual": ["Live"],
          "matched": true
        }
      ]
    }
  ],
  "warnings": [
    {
      "code": "missing_fact",
      "message": "分类事实缺少规则所需字段",
      "field": "music.secondary_types",
      "source": "musicbrainz"
    }
  ]
}
```

解释信息只在测试、名称识别调试和显式预览中完整返回；普通搜索和整理对象只保留规则 ID、revision 和结果，
避免扩大常规响应。

### 10.3 旧 API 兼容

兼容期只保留只读投影：

- `GET /api/v1/media/category`
- `GET /api/v1/media/category/config`

兼容规则：

1. `GET /media/category` 从新策略投影为 `{媒体类型: [分类路径]}`。
2. `GET /media/category/config` 把可表达为 legacy 结构的规则投影回 `movie/tv`；包含音乐或复杂条件时返回
   新结构标记，并提示旧客户端只读。
3. 旧 `POST /media/category/config` 在统一规则编辑器上线后移除；所有新写入只允许通过带
   `expected_revision` 的版本化分类策略 API。
4. `app.modules.themoviedb.CategoryHelper` 仅作为旧插件的只读导入兼容符号保留：读取结果投影自活动策略，
   `save()` 始终拒绝写入；官方 V3 插件改用 `app.sdk.classification.classify_media()`。

## 11. 前端交互设计

### 11.1 信息架构

分类策略继续从“设置 -> 目录”进入，但打开独立的大尺寸编辑器。桌面端采用三栏工作区，移动端按步骤切换：

```text
+----------------+-------------------------+----------------------+
| 媒体类型/分类树 | 当前分类的规则列表      | 规则条件检查器       |
| 电影            | 1. 日本动画            | 数据源：全部         |
| 电视剧          | 2. 国产剧              | 条件：全部满足       |
| 音乐            | 3. 未分类              | 字段/操作符/值       |
+----------------+-------------------------+----------------------+
| 校验结果 / 单条预览 / 发布前影响分析 / 保存发布                |
+----------------------------------------------------------------+
```

不得继续在每条规则中堆叠固定的 TMDB 表单控件。

### 11.2 分类树

- 顶部使用电影、电视剧、音乐三个标签页。
- 分类节点显示名称、目录面包屑、启用状态和规则数量。
- 支持新增子分类、复制、拖拽排序、重命名和停用。
- 删除被引用分类时展示引用位置，不允许直接删除。
- 目录路径逐段编辑，前端和后端同时校验非法字符。
- 每种媒体类型明确显示兜底分类，且只能有一个。

### 11.3 规则列表

- 卡片保持紧凑，展示规则名、来源范围、条件摘要、目标分类和启用状态。
- 拖拽改变首条命中顺序。
- 支持复制规则，复制后生成新稳定 ID。
- 支持只看错误、只看当前来源不可用字段、搜索规则名。
- 兜底分类在规则列表外单独展示；普通规则不允许使用空条件伪装 catch-all。

### 11.4 条件编辑器

每个条件行使用：

1. 字段选择器。
2. 操作符选择器。
3. 与字段类型匹配的值控件。
4. 删除按钮。

字段选择器按“通用、影视、音乐、数据源扩展”分组。值控件由字段目录动态决定：

- 枚举使用可搜索多选框。
- 国家、语言使用标准代码目录和本地化显示名。
- 年份使用数字或范围控件。
- 布尔使用切换项。
- 开放音乐流派和标签使用可创建选项的多选输入。

条件组使用 `全部满足`、`任一满足` 分段控件，并允许嵌套一层。复杂度上限由后端返回，首版建议最大深度 3、
每条规则最多 30 个叶子条件。

### 11.5 来源覆盖提示

选择来源范围后：

- `native/derived` 字段正常显示。
- `partial` 字段显示提示图标和具体说明。
- 全部所选来源均 `unavailable` 的字段禁止选择。
- 已保存规则因插件卸载变为不可用时，不删除规则，显示阻断错误并在发布时要求处理。

### 11.6 单条测试

支持三种测试输入：

1. 从名称测试结果直接带入当前媒体。
2. 选择 `media_source + media_id` 获取详情后测试。
3. 从最近整理历史选择一项重新求值。

测试面板显示：

- 标准分类事实。
- 自动推荐分类和最终有效分类。
- 命中规则。
- 每个条件的实际值和结果。
- 缺失字段与来源覆盖警告。
- 最终目录预览。

### 11.7 发布前影响分析

点击“应用”前可用草稿策略对最近 N 条整理历史或订阅样本执行只读比较：

- 分类未变化数量。
- 分类发生变化数量。
- 从有分类变为兜底或未分类数量。
- 按媒体类型和数据源分组的变化明细。
- 不执行文件移动，不修改历史和订阅。

变化较大时由用户二次确认，但不使用模糊的“可能有风险”提示，应明确列出影响数量和示例。

首期后端使用最近下载历史与成功整理历史合并抽样，按媒体身份去重，最多比较 200 条、返回 50 条
变化示例。历史表只稳定保存身份、类型、标题和年份，因此结果固定标记为估算并返回缺失事实警告；
`scanned/skipped/truncated` 和按来源、类型分组统计必须随响应返回。显式事实样本优先于历史抽样，
两种路径都只做纯求值，不触发数据源网络请求、文件移动、订阅修改或历史写入；批量求值离开 API
事件循环执行。

### 11.8 无障碍与移动端

- 拖拽排序必须同时提供“上移/下移”菜单操作。
- 所有图标按钮提供 tooltip 和可访问名称。
- 移动端使用“分类 -> 规则 -> 条件 -> 预览”的分层导航，不把三栏压缩到同一屏。
- 错误与警告不能只通过颜色表达。

## 12. 示例策略

### 12.1 电影动画

```json
{
  "kind": "category",
  "media_types": ["电影"],
  "sources": [],
  "when": {
    "all": [
      {"field": "media.genre_keys", "operator": "contains_any", "value": ["animation"]}
    ]
  },
  "target": {"category_id": "movie.animation"}
}
```

同一规则可匹配 TMDB 的 Genre ID 16、豆瓣类型名“动画”、Bangumi/AniList 对应类型投影，不在规则里写
任何来源专用字段。

### 12.2 日本动画剧集

```json
{
  "kind": "category",
  "media_types": ["电视剧"],
  "when": {
    "all": [
      {"field": "media.genre_keys", "operator": "contains_any", "value": ["animation"]},
      {"field": "media.countries", "operator": "contains_any", "value": ["JP"]}
    ]
  },
  "target": {"category_id": "tv.anime.jp"}
}
```

### 12.3 现场音乐专辑

```json
{
  "kind": "category",
  "media_types": ["音乐"],
  "when": {
    "all": [
      {"field": "music.entity_type", "operator": "in", "value": ["album", "recording"]},
      {"field": "music.secondary_types", "operator": "contains_any", "value": ["Live"]}
    ]
  },
  "target": {
    "category_id": "music.live",
    "labels": ["现场"]
  }
}
```

### 12.4 影视原声

```json
{
  "kind": "category",
  "media_types": ["音乐"],
  "when": {
    "any": [
      {"field": "music.secondary_types", "operator": "contains_any", "value": ["Soundtrack"]},
      {"field": "music.tags", "operator": "contains_any", "value": ["soundtrack", "ost"]}
    ]
  },
  "target": {"category_id": "music.soundtrack"}
}
```

### 12.5 来源专用扩展条件

```json
{
  "kind": "category",
  "media_types": ["电影"],
  "sources": ["example.source"],
  "when": {
    "all": [
      {
        "field": "extensions.example.source.region_group",
        "operator": "equals",
        "value": "east_asia"
      }
    ]
  },
  "target": {"category_id": "movie.east_asia"}
}
```

## 13. Legacy 迁移方案

### 13.1 自动迁移

启动时仅在 `MediaClassificationPolicy` 不存在时执行：

1. 读取现有 `CONFIG_PATH/category.yaml`。
2. 按 YAML 顺序转换 `movie`、`tv` 分类。
3. 为每个分类生成稳定 ID；相同名称通过规范化后附加短摘要避免冲突。
4. `genre_ids` 转换为 `media.genre_keys`；暂时无法映射的 ID 保存为
   `extensions.themoviedb.genre_ids` 兼容条件并发出提示。
5. 仅当标准事实与 legacy 真值语义可证明一致时，才把字段提升为 `media.*` 条件；否则使用统一的
   `extensions.themoviedb.*` 旧比较视图。该视图保留假值缺失、字符串大写、列表逐元素字符串化、
   `production_countries.iso_3166_1` 和 `release_date/first_air_date` 年份截取语义。
6. `original_language`、`production_countries`、`origin_country`、`release_year` 等字段若因标准化回退、
   类型转换或假值处理可能改变旧结果，必须保留为受控 TMDB 扩展事实，不能为了使用标准字段牺牲等价性。
7. 其它 TMDB 一级字段转换到受控 `extensions.themoviedb.*` 字段；无法登记的字段阻止自动发布，保留
   legacy 运行并提示管理员处理。
8. 首个空规则分类转换为 `source_fallbacks.themoviedb` 下该媒体类型的来源级兜底，禁止污染豆瓣、
   IMDb 等其它来源；其后的 legacy 项在旧实现中本就不可达，迁移时保持禁用并向管理员报告。
9. 新策略仍为电影、电视剧和音乐配置通用兜底，用于没有来源级兜底或来源级规则未命中的情况。
10. 保存新策略 revision 1，并保留原 YAML 文件只读备份，不再继续写入。

### 13.2 行为兼容

- 迁移测试必须证明同一批 TMDB fixture 在旧分类器和新分类器下得到完全相同的目录分类。
- 旧配置中的 `!值` 转换为明确的排除操作符。
- 旧逗号字符串转换为数组，后续 API 不再使用逗号编码多值。
- 迁移后 TMDB 模块不再实例化 `CategoryHelper`，也不再写 `MediaInfo.category`。

### 13.3 字段迁移

建议分两个版本完成：

1. 第一个版本同时写 `library_category` 和兼容 `category`，读侧优先新字段。
2. 前端、目录、订阅、下载、历史、通知全部迁移后，`category` 只保留序列化兼容属性。
3. 音乐 `category` 来源数据迁入 `metadata_category`，音乐 UI 不再读取兼容 `category` 展示专辑类型。

## 14. 校验规则

服务端发布前必须拒绝：

- 重复分类 ID、规则 ID、同媒体类型重复路径。
- 不存在的目标分类或跨媒体类型目标。
- 空分类名称、非法路径段、目录穿越片段。
- 未登记字段或字段不支持的操作符。
- 条件值类型错误、范围起止颠倒、空 `in` 列表。
- 递归深度、规则数或条件数超过上限。
- 普通规则条件为空；兜底分类必须通过 `fallbacks` 表达。
- 同一媒体类型没有可用兜底分类。
- 插件扩展字段的命名空间与来源不一致。
- 规则引用已卸载插件且没有兼容处理。

服务端应警告但允许发布：

- 某字段仅被部分来源支持。
- 两条规则可能有重叠，但顺序可以明确决定结果。
- 分类路径发生变化，可能影响未来整理目标。
- 当前策略没有覆盖某个已注册但未启用的数据源。

## 15. 测试方案

### 15.1 后端单元测试

- 条件操作符和缺失值三态语义。
- `all/any/not` 嵌套、最大深度和短路行为。
- 首条命中、兜底、标签累积和人工覆盖优先级。
- 分类 ID、路径和引用完整性校验。
- 策略 revision 冲突和回滚单调递增。
- 分类事实构造不修改原始 `MediaInfo` / `MusicInfo`。

### 15.2 来源契约测试

至少覆盖：

- TMDB：Genre ID、语言、电影出品国、电视剧原产国、年份。
- 豆瓣：类型名、国家、年份。
- Bangumi：标签、平台、工作室、年份。
- AniList：Genre、国家、格式、年份。
- IMDb、TVDB：当前统一字段投影可用部分。
- MusicBrainz：专辑主类型、副类型、流派、标签、发行状态。
- TheAudioDB：流派、风格、专辑和艺术家信息。
- 豆瓣音乐：专辑、艺术家、流派和发行信息的可用部分。
- 一个同时支持影视和音乐的插件来源。

来源能力声明必须与实际 fixture 投影一致，避免 UI 显示支持但运行时永远缺失。

### 15.3 工作流回归测试

- 同步和异步识别得到相同分类。
- 按标题、按 ID、缓存命中和插件识别都经过分类收口。
- 音乐 `metadata_category` 不会被当成媒体库分类。
- 目录按分类 ID 匹配，改名后引用不失效。
- 多级分类路径安全生成。
- 订阅人工分类覆盖自动分类。
- 下载历史和整理历史保存实际分类快照。
- durable transfer 恢复使用计划中的 revision 和路径，不受策略更新影响。
- legacy `category.yaml` fixture 迁移前后 TMDB 分类完全一致。

### 15.4 前端测试

- 字段和操作符完全由后端能力目录驱动。
- 电影、电视剧、音乐规则的新增、复制、排序、删除和保存。
- 来源 `partial/unavailable` 提示。
- 分类被引用时禁止删除。
- revision 冲突时保留本地草稿并提供重新加载/合并选择。
- 预览展示实际值、失败条件和最终目录。
- 桌面和窄屏布局不重叠，键盘可完成排序和编辑。

### 15.5 性能目标

- 200 条规则、每条平均 6 个条件时，单次纯求值 P95 小于 5ms。
- 常规识别路径不因分类新增额外网络请求。
- 影响分析分批执行并限制样本数量，不阻塞 API 事件循环。
- 策略发布后使用不可变快照替换，不在每次分类时解析 JSON。

## 16. 分阶段实施

### 阶段 A：语义和领域基础

1. 新增 `library_category`、`metadata_category`、`ClassificationFacts` 和 `ClassificationResult`。
2. 将音乐来源描述性 `category` 迁移到 `metadata_category`。
3. 实现纯规则模型、求值器和校验器。
4. 建立标准字段和来源能力目录。

### 阶段 B：配置与兼容

1. 新增 `MediaClassificationPolicy` SystemConfig。
2. 实现策略服务、revision、历史和回滚。
3. 实现 `category.yaml` 自动迁移。
4. 保留旧 GET API 投影，统一编辑器上线后移除 legacy 客户端写入口。

### 阶段 C：识别与整理接入

1. 从 TheMovieDb 模块移除分类副作用。
2. 统一所有识别结果收口。
3. 迁移目录选择、订阅、下载和历史消费者到分类 ID + 路径快照。
4. 在 durable transfer 计划中冻结分类结果。

### 阶段 D：前端规则编辑器

1. 用动态字段编辑器替换 TMDB 专用弹窗。
2. 支持音乐、来源范围和组合条件。
3. 增加单条预览、校验解释和影响分析。
4. 目录设置改为保存分类 ID，展示当前路径。

### 阶段 E：插件和可选跨源补充

1. 扩展插件媒体来源注册协议和文档。
2. 支持受控扩展事实。
3. 根据实际需要增加 `enrich_missing`，默认仍关闭。
4. 移除旧 YAML 写入口和 TMDB 内部 CategoryHelper 依赖，仅保留旧插件只读导入兼容符号。

## 17. 验收标准

以下条件全部满足才可认为新体系完成：

1. 电影、电视剧、音乐都能在同一 UI 中配置自动分类。
2. TMDB、豆瓣、Bangumi、AniList、MusicBrainz、TheAudioDB、豆瓣音乐以及插件来源至少各有
   一条真实 fixture 通过分类测试。
3. 规则引擎和 Application 分类服务不导入具体来源模块。
4. 同步、异步、缓存和插件识别分类结果一致。
5. `media_source + media_id` 在分类前后保持不变。
6. 音乐来源分类不会直接成为目录分类。
7. 分类改名不破坏目录、订阅和规则引用。
8. 发布策略前能看到校验结果和近期样本影响。
9. 运行中的整理任务在策略更新后仍使用原计划目标。
10. 现有 `category.yaml` 可以自动迁移，TMDB 旧规则行为无回归。

### 17.1 落地验收证据

| 验收项 | 自动化证据 | 状态 |
| --- | --- | --- |
| 统一 UI 支持电影、电视剧、音乐 | 前端 `AccountSettingClassification`、分类树、规则与条件构建器测试 | 通过 |
| 内置与插件来源 fixture | `test_media_classification_source_contracts.py`；官方 LunaTVSource CMS 分类集成测试 | 通过 |
| 分类层不导入具体来源 | `test_media_classification_architecture.py` 静态扫描 Domain/Application 分类包 | 通过 |
| 同步、异步、缓存、插件结果一致 | `test_media_classification_execution.py`、`test_media_classification_plugin_extensions.py`、LunaTVSource 同异步测试 | 通过 |
| 分类前后身份不变 | 分类事实、执行服务、插件事实与缓存往返测试 | 通过 |
| 音乐来源分类不成为目录分类 | 分类事实、执行服务和官方插件历史快照测试 | 通过 |
| 改名不破坏稳定引用 | 配置服务、目录解析和订阅引用测试 | 通过 |
| 发布前校验与影响分析 | 分类 API 测试；前端发布控制、影响面板和 revision 冲突测试 | 通过 |
| Durable plan 冻结原目标 | `test_media_classification_transfer_checkpoint.py` 的 planned replay 与 provider promotion 测试 | 通过 |
| YAML 自动迁移且旧规则等价 | `test_media_classification_startup.py` 与 `test_media_classification_legacy.py` | 通过 |

最终验收于 2026-09-03 完成：后端 `8149 passed, 9 skipped`；前端 249 个测试文件、2475 项测试通过；官方插件仓 CI 55 项、V3 580 项、兼容 V2 39 项通过。宿主与插件架构基线、严格 mypy、Ruff/mypy 低水位、事件、并发、异步阻塞、复杂度、schema 导出、Agent API 审计、Alembic 唯一 head 和启动性能门禁全部通过。

## 18. 推荐决策摘要

| 决策项 | 推荐方案 |
| --- | --- |
| 分类所有权 | 独立 Domain + Application 能力，不属于任何数据源模块 |
| 配置存储 | `SystemConfigKey.MediaClassificationPolicy` 版本化整体存储 |
| 求值模式 | 有序首条命中，一个主分类，可附加多个标签 |
| 多来源规则 | 优先使用标准事实；来源特有字段使用受控命名空间 |
| 音乐字段 | `metadata_category` 与 `library_category` 强制分离 |
| 分类引用 | 稳定 `category_id` + 可变目录路径快照 |
| 前端表单 | 后端字段目录驱动的条件构建器，不硬编码 TMDB 字段 |
| 默认跨源行为 | `primary_only`，可选 `enrich_missing` 后续提供 |
| 可解释性 | 保存 rule ID/revision，预览按条件返回 trace |
| 兼容策略 | 自动迁移 YAML；旧 GET 保留只读投影，旧 POST 在统一编辑器上线后移除 |
