"""每个 harness 一个适配器。

三个 harness 的配置载体、字段名、优先级规则都不一样，必须分流处理，
不能把某一个的字段名假设套到另外两个上。
"""
from __future__ import annotations

from typing import List

from .base import HarnessAdapter, HarnessConfig, ResolvedField, FileState, LayerValue
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek import DeepSeekHarnessAdapter

ALL_ADAPTERS: List[HarnessAdapter] = [
    ClaudeCodeAdapter(),
    CodexAdapter(),
    DeepSeekHarnessAdapter(),
]


def detect_installed(env=None, home=None, project_dir=None) -> List[HarnessAdapter]:
    """探测本机装了哪些 harness。装了哪个就体检哪个；一个都没探测到时
    返回空列表，由上层如实提示，而不是硬猜一个去读。"""
    return [a for a in ALL_ADAPTERS if a.detect(env=env, home=home, project_dir=project_dir)]


def get_adapter(harness_id: str) -> HarnessAdapter:
    for a in ALL_ADAPTERS:
        if a.harness_id == harness_id:
            return a
    raise KeyError(harness_id)


__all__ = [
    "HarnessAdapter", "HarnessConfig", "ResolvedField", "FileState", "LayerValue",
    "ALL_ADAPTERS", "detect_installed", "get_adapter",
]
