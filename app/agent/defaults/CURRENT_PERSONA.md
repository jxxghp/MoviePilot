---
version: 3
active_persona: default
extra_context_files: []
deprecated_phrases: []
---
# CURRENT_PERSONA

当前激活人格：`default`

运行时加载顺序固定如下：

1. 核心系统提示词（程序内置，不可运行时覆盖）
2. `personas/<active_persona>/PERSONA.md`
3. `extra_context_files`
4. `memory/MEMORY.md`（默认注入）
5. `memory/<topic>.md` 与 `memory/activity/*.md`（通过 search_memory 按需检索）

`memory` 中的长期偏好可以细化回复方式，但不应覆盖系统核心身份、目标和安全边界。
