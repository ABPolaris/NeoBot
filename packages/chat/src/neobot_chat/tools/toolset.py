from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from neobot_chat.schema.exceptions import ToolError
from neobot_chat.schema.protocol import ToolExecutor
from neobot_chat.schema.types import (
    ToolAccessPolicy,
    ToolAccessRule,
    ToolDefinition,
    ToolGuardContext,
)
from neobot_chat.tools.composite import CompositeToolExecutor

AccessResolver = Callable[[dict, ToolGuardContext, ToolAccessPolicy], ToolAccessRule]


@dataclass(frozen=True)
class ToolSpec:
    definition: ToolDefinition
    access_resolver: AccessResolver

    @property
    def name(self) -> str:
        return self.definition["function"]["name"]


class MergedToolExecutor(ToolExecutor):
    """把多个 Toolset 组合成一个执行面。

    契约是各 Toolset 的 definitions()（公开面），而不是 specs 快照：
    LiveToolset 一类工具集会按需加载，合并后必须继续实时反映当前工具面。
    重名工具后者优先，执行路由到当前定义它的 Toolset。
    """

    def __init__(self, toolsets: list["Toolset"]):
        self._toolsets = list(toolsets)

    def _surface(self) -> dict[str, tuple["Toolset", ToolDefinition]]:
        surface: dict[str, tuple[Toolset, ToolDefinition]] = {}
        for toolset in self._toolsets:
            for definition in toolset.definitions():
                surface[definition["function"]["name"]] = (toolset, definition)
        return surface

    def definitions(self) -> list[ToolDefinition]:
        return [definition for _toolset, definition in self._surface().values()]

    async def execute(self, name: str, args: dict) -> str:
        entry = self._surface().get(name)
        if entry is None:
            raise ToolError(f"Unknown tool: {name}")
        return await entry[0].executor.execute(name, args)

    async def close(self) -> None:
        for toolset in self._toolsets:
            await toolset.executor.close()


@dataclass(frozen=True)
class Toolset:
    executor: ToolExecutor
    specs: list[ToolSpec] = field(default_factory=list)
    policy: ToolAccessPolicy = field(default_factory=ToolAccessPolicy)

    def definitions(self) -> list[ToolDefinition]:
        return [spec.definition for spec in self.specs]

    @classmethod
    def merge(cls, toolsets: list[Toolset | None]) -> Toolset:
        """合并工具集：以各自的 definitions() 为准，而不是 specs 快照。

        动态工具集（如按需加载）的 specs 为空，但 definitions() 会随加载变化；
        合并结果必须保留这份实时性，否则 Agent 拿到的是一个没有工具的空壳。
        """
        active_toolsets = [toolset for toolset in toolsets if toolset is not None]
        if not active_toolsets:
            return cls(executor=CompositeToolExecutor([]), specs=[])

        # specs 只用于访问策略（静态工具才有 resolver），不参与工具面判定。
        selected_specs: dict[str, tuple[ToolSpec, int]] = {}
        for index, toolset in enumerate(active_toolsets):
            for spec in toolset.specs:
                selected_specs[spec.name] = (spec, index)

        ordered_specs: list[ToolSpec] = []
        seen_names: set[str] = set()
        for toolset in reversed(active_toolsets):
            for spec in reversed(toolset.specs):
                selected = selected_specs.get(spec.name)
                if selected is None or selected[0] is not spec or spec.name in seen_names:
                    continue
                seen_names.add(spec.name)
                ordered_specs.append(spec)
        ordered_specs.reverse()

        merged_executor = MergedToolExecutor(active_toolsets)
        return MergedToolset(executor=merged_executor, specs=ordered_specs)


class MergedToolset(Toolset):
    """合并后的实时工具集：definitions() 每轮从合并执行器重算。"""

    def definitions(self) -> list[ToolDefinition]:
        return self.executor.definitions()
