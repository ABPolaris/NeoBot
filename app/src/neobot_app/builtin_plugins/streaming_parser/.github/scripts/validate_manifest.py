"""校验插件清单与入口，规则对齐 NeoBot 的 neobot_modloader。

可本地运行：python .github/scripts/validate_manifest.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "plugin.toml"
ENTRY = ROOT / "__init__.py"

NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_TAG_COUNT = 16
MAX_TAG_LENGTH = 32

errors: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    if not MANIFEST.is_file():
        print("plugin.toml 不存在")
        return 1
    with MANIFEST.open("rb") as file:
        manifest = tomllib.load(file)

    name = manifest.get("name")
    check(isinstance(name, str) and bool(name), "plugin.toml 缺少 name")
    if isinstance(name, str):
        check(NAME_PATTERN.fullmatch(name) is not None, f"name 不合法: {name!r}")
        check("__" not in name, f"name 不能包含双下划线: {name!r}")
        # 注意：插件名不要求等于仓库名（例如仓库 NeoBot_Dashboard 的插件名是 dashboard），
        # NeoBot 只要求 plugin.toml 的 name 与 Plugin(...) 的 name 一致。

    version = manifest.get("version")
    check(isinstance(version, str) and bool(version), "plugin.toml 缺少 version")

    for key in ("enabled", "hot_reload", "config_hot_reload"):
        if key in manifest:
            check(isinstance(manifest[key], bool), f"{key} 必须是 bool")

    if "priority" in manifest:
        check(isinstance(manifest["priority"], int), "priority 必须是 int")

    if "dependencies" in manifest:
        deps = manifest["dependencies"]
        check(isinstance(deps, list) and all(isinstance(d, str) for d in deps),
              "dependencies 必须是 string list")

    for key in ("python_dependencies", "pypi_dependencies", "requirements"):
        if key in manifest:
            values = manifest[key]
            check(isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values),
                  f"{key} 必须是非空 string list")

    if "tags" in manifest:
        tags = manifest["tags"]
        check(isinstance(tags, list) and all(isinstance(t, str) for t in tags), "tags 必须是 string list")
        if isinstance(tags, list):
            check(len(tags) <= MAX_TAG_COUNT, f"tags 最多 {MAX_TAG_COUNT} 项")
            check(all(len(t) <= MAX_TAG_LENGTH for t in tags), f"tags 单项不能超过 {MAX_TAG_LENGTH} 字符")

    if "config" in manifest:
        check(isinstance(manifest["config"], dict), "[config] 必须是 table")

    check(ENTRY.is_file(), "__init__.py 不存在")
    if ENTRY.is_file():
        source = ENTRY.read_text(encoding="utf-8")
        check("plugin = Plugin(" in source, "__init__.py 必须导出 plugin = Plugin(...)")
        if isinstance(name, str):
            check(f'"{name}"' in source, f"__init__.py 中应出现 Plugin({name!r})")
        if isinstance(version, str):
            check(f'"{version}"' in source, f"__init__.py 中应出现 version={version!r}")

    if errors:
        print("插件清单校验失败：")
        for item in errors:
            print(f"  - {item}")
        return 1
    print(f"插件清单校验通过: {name} {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
