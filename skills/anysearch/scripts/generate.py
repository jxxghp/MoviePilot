#!/usr/bin/env python3
"""
根据共享常量生成 AnySearch 各语言命令行脚本的固定代码段。

从 scripts/shared/constants.json 读取领域列表，并更新四种语言的常量与文档命令。
使用 --check 只核对产物是否最新，不写入文件。
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_DIR = os.path.join(SCRIPT_DIR, "shared")

# 各语言用成对标记限定生成区域，标记文本本身是生成器协议，不可翻译：
#   BEGIN GENERATED:<section_name>
#   ... 生成内容 ...
#   END GENERATED:<section_name>

MARKERS = {
    ".py": ("# BEGIN GENERATED:{name}", "# END GENERATED:{name}"),
    ".js": ("// BEGIN GENERATED:{name}", "// END GENERATED:{name}"),
    ".ps1": ("# BEGIN GENERATED:{name}", "# END GENERATED:{name}"),
    ".sh": ("# BEGIN GENERATED:{name}", "# END GENERATED:{name}"),
}


def load_constants():
    """
    读取生成各语言脚本所需的共享常量。

    :return: 包含可用搜索领域的配置字典
    """
    with open(os.path.join(SHARED_DIR, "constants.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def render_constants(ext, constants):
    """
    按目标语言语法生成领域常量声明。

    :param ext: 目标脚本的文件扩展名
    :param constants: 共享领域常量
    :return: 可插入生成区域的源码文本
    """
    domains = constants["available_domains"]

    if ext == ".py":
        lines = []
        lines.append("AVAILABLE_DOMAINS = [")
        for i in range(0, len(domains), 6):
            chunk = domains[i:i+6]
            lines.append("    " + ", ".join(f'"{d}"' for d in chunk) + ",")
        lines.append("]")
        return "\n".join(lines)

    elif ext == ".js":
        lines = []
        lines.append("const AVAILABLE_DOMAINS = [")
        for i in range(0, len(domains), 6):
            chunk = domains[i:i+6]
            lines.append("  " + ",".join(f'"{d}"' for d in chunk) + ",")
        lines.append("];")
        return "\n".join(lines)

    elif ext == ".ps1":
        lines = []
        lines.append("$AVAILABLE_DOMAINS = @(")
        chunks = [domains[i:i+6] for i in range(0, len(domains), 6)]
        for idx, chunk in enumerate(chunks):
            suffix = "," if idx < len(chunks) - 1 else ""
            lines.append("    " + ", ".join(f'"{d}"' for d in chunk) + suffix)
        lines.append(")")
        return "\n".join(lines)

    elif ext == ".sh":
        lines = []
        lines.append("AVAILABLE_DOMAINS=(" + " ".join(f'"{d}"' for d in domains) + ")")
        return "\n".join(lines)

    raise ValueError(f"Unsupported extension: {ext}")


def render_doc_block(ext, constants):
    """
    生成运行时读取并渲染 doc_spec.md 的代码段。

    :param ext: 目标脚本的文件扩展名
    :param constants: 共享常量；当前代码段运行时再读取最新值
    :return: 对应语言的文档命令源码
    """
    if ext == ".py":
        return '''def _render_doc():
    """
    根据共享模板渲染当前语言的接口说明。

    :return: 已替换语言和领域占位符的说明文本
    """
    import json as _json
    _dir = os.path.dirname(os.path.abspath(__file__))
    _shared = os.path.join(_dir, "shared")
    with open(os.path.join(_shared, "doc_spec.md"), "r", encoding="utf-8") as _f:
        _tpl = _f.read()
    with open(os.path.join(_shared, "constants.json"), "r", encoding="utf-8") as _f:
        _c = _json.load(_f)
    _tpl = _tpl.replace("{{LANG_NAME}}", "Python")
    _tpl = _tpl.replace("{{LANG_CODEBLOCK}}", "")
    _tpl = _tpl.replace("{{LANG_INVOKE}}", "python scripts/anysearch_cli.py")
    _tpl = _tpl.replace("{{DOMAINS_SPACE}}", " ".join(_c["available_domains"]))
    return _tpl'''

    elif ext == ".js":
        return '''function renderDoc() {
  const shared = path.join(__dirname, "shared");
  let tpl = fs.readFileSync(path.join(shared, "doc_spec.md"), "utf-8");
  const c = JSON.parse(fs.readFileSync(path.join(shared, "constants.json"), "utf-8"));
  tpl = tpl.replace(/\\{\\{LANG_NAME\\}\\}/g, "Node.js");
  tpl = tpl.replace(/\\{\\{LANG_CODEBLOCK\\}\\}/g, "");
  tpl = tpl.replace(/\\{\\{LANG_INVOKE\\}\\}/g, "node scripts/anysearch_cli.js");
  tpl = tpl.replace(/\\{\\{DOMAINS_SPACE\\}\\}/g, c.available_domains.join(" "));
  return tpl;
}'''

    elif ext == ".ps1":
        return '''function Render-Doc {
    $shared = Join-Path (Split-Path -Parent $MyInvocation.ScriptName) "shared"
    $tpl = Get-Content (Join-Path $shared "doc_spec.md") -Raw -Encoding UTF8
    $c = Get-Content (Join-Path $shared "constants.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $tpl = $tpl.Replace("{{LANG_NAME}}", "PowerShell")
    $tpl = $tpl.Replace("{{LANG_CODEBLOCK}}", "powershell")
    $tpl = $tpl.Replace("{{LANG_INVOKE}}", "powershell -ExecutionPolicy Bypass -File scripts/anysearch_cli.ps1")
    $tpl = $tpl.Replace("{{DOMAINS_SPACE}}", ($c.available_domains -join " "))
    return $tpl
}'''

    elif ext == ".sh":
        return r'''_cmd_doc() {
  local shared="$SCRIPT_DIR/shared"
  local tpl
  tpl=$(cat "$shared/doc_spec.md")
  local domains
  domains=$(jq -r '.available_domains | join(" ")' "$shared/constants.json")
  tpl="${tpl//\{\{LANG_NAME\}\}/Bash}"
  tpl="${tpl//\{\{LANG_CODEBLOCK\}\}/bash}"
  tpl="${tpl//\{\{LANG_INVOKE\}\}/bash scripts/anysearch_cli.sh}"
  tpl="${tpl//\{\{DOMAINS_SPACE\}\}/$domains}"
  printf '%s\n' "$tpl"
}'''

    raise ValueError(f"Unsupported extension: {ext}")


def replace_marker_section(content, ext, section_name, new_text):
    """
    替换指定生成标记之间的内容，保留标记本身。

    :param content: 目标脚本的完整源码
    :param ext: 用于选择标记语法的文件扩展名
    :param section_name: 待替换生成区域的名称
    :param new_text: 新区域源码
    :return: 替换后的完整源码
    """
    begin_tag, end_tag = MARKERS[ext]
    begin = begin_tag.format(name=section_name)
    end = end_tag.format(name=section_name)

    if begin not in content:
        raise ValueError(f"BEGIN marker '{begin_tag.format(name=section_name)}' not found")
    if end not in content:
        raise ValueError(f"END marker '{end_tag.format(name=section_name)}' not found")

    before, rest = content.split(begin, 1)
    _, after = rest.split(end, 1)
    return before + begin + "\n" + new_text + "\n" + end + after


def generate_script(script_path, constants):
    """
    更新单个语言脚本中的常量与文档生成区域。

    :param script_path: 待更新脚本路径
    :param constants: 共享领域常量
    :return: 更新后的完整源码，不直接写入文件
    """
    ext = os.path.splitext(script_path)[1]
    if ext not in MARKERS:
        raise ValueError(f"Unsupported extension: {ext}")

    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()

    constants_text = render_constants(ext, constants)
    content = replace_marker_section(content, ext, "CONSTANTS", constants_text)

    doc_block = render_doc_block(ext, constants)
    content = replace_marker_section(content, ext, "DOC_SPEC", doc_block)

    return content


def main():
    """
    批量更新各语言脚本，或只核对生成内容是否最新。
    """
    import argparse

    parser = argparse.ArgumentParser(description="Generate AnySearch CLI scripts from shared data")
    parser.add_argument("--check", action="store_true", help="Verify scripts are up-to-date (for CI)")
    args = parser.parse_args()

    constants = load_constants()

    scripts_changed = False

    for ext in [".py", ".js", ".ps1", ".sh"]:
        script_name = f"anysearch_cli{ext}"
        script_path = os.path.join(SCRIPT_DIR, script_name)

        try:
            new_content = generate_script(script_path, constants)
            with open(script_path, "r", encoding="utf-8") as f:
                old_content = f.read()

            if new_content != old_content:
                scripts_changed = True
                if not args.check:
                    with open(script_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    print(f"Generated: {script_name}")
                else:
                    print(f"CHANGED: {script_name} (run generate.py to update)")
            else:
                print(f"OK: {script_name}")
        except Exception as e:
            print(f"ERROR in {script_name}: {e}", file=sys.stderr)
            sys.exit(1)

    if args.check and scripts_changed:
        sys.exit(1)


if __name__ == "__main__":
    main()
