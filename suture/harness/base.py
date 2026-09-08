"""适配器接口与三个 harness 共用的数据结构。

检测层只认这里定义的结构，不认任何一个 harness 的原始字段名——这样
checks.py 才能保持与 harness 无关，不必为每个 harness 写一套判断。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 三个逻辑字段。各 harness 把自己的原始字段名映射到这三个上。
FIELD_BASE_URL = "base_url"
FIELD_AUTH = "auth"
FIELD_MODEL = "model"
LOGICAL_FIELDS = [FIELD_BASE_URL, FIELD_AUTH, FIELD_MODEL]


@dataclass
class LayerValue:
    """某一层配置里这个字段的取值。"""
    layer: str          # 层的名字，比如 "环境变量" / "项目级配置"
    path: str           # 来源文件路径；环境变量层为空字符串
    value: str
    effective: bool = False   # 是不是实际生效的那一层


@dataclass
class FileState:
    """一个配置文件的读取结果。"""
    layer: str
    path: str
    exists: bool
    parse_ok: bool = True
    parse_error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    unknown_keys: List[str] = field(default_factory=list)
    active: bool = True       # 这一层是否真的会被 harness 采纳（Codex 的可信项目机制会让它变成 False）
    inactive_reason: str = ""


@dataclass
class ResolvedField:
    """一个逻辑字段跨所有层解析后的结果。"""
    key: str
    value: Optional[str]
    source_layer: str = ""
    source_path: str = ""
    layers: List[LayerValue] = field(default_factory=list)

    @property
    def is_set(self) -> bool:
        return bool(self.value)

    def differing_layers(self) -> List[LayerValue]:
        """取值与生效值不同的其它层。用于「多层配置取值不一致」这条检测。"""
        if not self.value:
            return []
        return [lv for lv in self.layers if not lv.effective and lv.value != self.value]


@dataclass
class ExtraAuthHeader:
    """通过跟主鉴权字段平行的另一种机制（比如自定义请求头环境变量）
    额外发现的、同样可能承载鉴权的请求头。

    这跟 FIELD_AUTH 解析出来的主字段不是替代关系：真实客户端两个都会发，
    Suture 不应该替用户决定网关到底认哪一个——探活/真实请求要把两者都带上，
    检测层则要如实提示"这里同时有两个来源在生效"。"""
    header: str        # 请求头名字，比如 "Token"
    value: str
    source: str         # 从哪个变量/文件来的，展示用，比如 "ANTHROPIC_CUSTOM_HEADERS"


@dataclass
class HarnessConfig:
    """一个 harness 体检所需的全部输入。"""
    harness_id: str
    display_name: str
    fields: Dict[str, ResolvedField] = field(default_factory=dict)
    files: List[FileState] = field(default_factory=list)

    # 鉴权的两种结构：直接存值，或者存一个环境变量名（间接引用）
    auth_is_indirect: bool = False
    auth_env_name: Optional[str] = None       # 间接引用时，配置里写的那个变量名
    auth_env_resolved: bool = False           # 那个变量名对应的环境变量是否取到了值
    auth_header: str = "x-api-key"            # 这个客户端实际会发哪个请求头
    auth_conflict: Optional[str] = None       # 两处同时填了鉴权信息时的说明
    extra_auth_headers: List[ExtraAuthHeader] = field(default_factory=list)

    # 鉴权值可能存在多个不同名字的变量里（Claude Code 的 ANTHROPIC_API_KEY 和
    # ANTHROPIC_AUTH_TOKEN 就是两个都认的位置，用哪个决定了客户端发哪个请求头）。
    # 修复鉴权时必须改「当前真的在生效的那个变量」，不能写死其中一个，否则会
    # 凭空新建一个变量、把请求头悄悄换掉，等于制造一个新冲突。
    auth_env_var: Optional[str] = None        # 当前生效的鉴权值存在哪个环境变量里
    auth_rival_env_vars: List[str] = field(default_factory=list)  # 同时设置着的其它鉴权变量

    # DeepSeek Harness 的模型是一份注册清单而不是单个选中值，单独放在这里，
    # 每一项都要对着网关路由表校验。另外两个 harness 这里为空。
    model_candidates: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)   # 仅供参考的信息，不是错误

    @property
    def has_any_config(self) -> bool:
        if any(f.is_set for f in self.fields.values()):
            return True
        return any(f.exists for f in self.files)

    def field(self, key: str) -> ResolvedField:
        return self.fields.get(key, ResolvedField(key=key, value=None))


class HarnessAdapter:
    """所有 harness 适配器的接口。"""

    harness_id: str = ""
    display_name: str = ""
    config_format: str = ""      # "json" / "toml" / "yaml"，用于语法检测的措辞

    # ---- 探测 ----
    def detect(self, env=None, home=None, project_dir=None) -> bool:
        """本机是否装了/配置过这个 harness。"""
        raise NotImplementedError

    # ---- 读取 ----
    # 签名要跟三个实现和所有调用点保持一致：known_keys 是规则文件里登记的合法配置项
    # （用来识别写错的键名），accepted_headers 是网关认的鉴权请求头名单（用来判断
    # 自定义头里哪些算鉴权来源）。之前基类少了这两个参数，照基类签名调会 TypeError。
    def read(self, env=None, home=None, project_dir=None,
             known_keys=None, accepted_headers=None) -> HarnessConfig:
        raise NotImplementedError

    # ---- 写入 ----
    def writable_paths(self, cfg: HarnessConfig) -> List[str]:
        """修复时可能会改到的文件，修复前要先备份这些。"""
        raise NotImplementedError

    def apply(self, cfg: HarnessConfig, changes: Dict[str, str],
              env=None, home=None, project_dir=None) -> List[str]:
        """把 {逻辑字段: 新值} 写回配置。返回给用户看的改动说明。"""
        raise NotImplementedError

    def generate_minimal_config(self, base_url: str, model: str,
                                env=None, home=None, project_dir=None) -> str:
        """全新用户从零生成一份最小可用配置，返回写入的文件路径。"""
        raise NotImplementedError

    # ---- 持久化环境变量 ----
    # 只有像 Claude Code CLI 这种「环境变量优先于配置文件」的 harness 才需要实现这三个；
    # 默认当作没有这一层——Codex/DeepSeek 的 base_url/model 从不来自环境变量，用默认值即可，
    # 不需要每个适配器都写一遍空实现。
    def env_var_targets(self, cfg: HarnessConfig,
                        changes: Dict[str, str]) -> Dict[str, Optional[str]]:
        """给定这次要写的 {逻辑字段: 新值}，挑出其中「当前实际生效层就是环境变量」的那些，
        映射成 {环境变量名: 新值}。这是一次纯读取、不做任何修改的预览，
        用来在真正写之前决定该备份哪些环境变量、真正写的时候该改哪一层。

        值为 None 表示「这个变量要被删掉」——鉴权同时填在两个变量里时，
        统一到一处就意味着留一个、删另一个。删掉的那个也在这份清单里，
        所以它的原值一样会被备份，回滚时能恢复回来。"""
        return {}

    def supports_persistent_env(self) -> bool:
        """本平台能不能真的改持久化环境变量。改不了的时候要在动手之前就知道，
        好如实告诉用户该自己去设哪个变量，而不是写到一半抛异常。"""
        return False

    def read_persistent_env(self, name: str) -> Optional[str]:
        """读取某个环境变量当前持久化存储（注册表/shell 配置）里的值，
        不是当前进程的 os.environ 快照——同一个已经在运行的进程改了环境变量后，
        os.environ 不会自动刷新，验证修复是否生效必须靠这个读到最新值。"""
        return None

    def write_persistent_env(self, name: str, value: str) -> None:
        raise NotImplementedError

    def unset_persistent_env(self, name: str) -> None:
        raise NotImplementedError


# ---- 各适配器共用的小工具 ----

def resolve_home(home: Optional[str], env: Optional[Dict[str, str]] = None) -> str:
    if home:
        return home
    env = env if env is not None else os.environ
    return env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")


def resolve_project_dir(project_dir: Optional[str]) -> str:
    return project_dir or os.getcwd()


def build_resolved(key: str, layers_in_priority_order: List[LayerValue]) -> ResolvedField:
    """按优先级从高到低传入各层取值，产出解析结果。"""
    rf = ResolvedField(key=key, value=None)
    for lv in layers_in_priority_order:
        if lv.value:
            rf.layers.append(lv)
    for lv in rf.layers:
        if rf.value is None:
            rf.value = lv.value
            rf.source_layer = lv.layer
            rf.source_path = lv.path
            lv.effective = True
    return rf


def mask_secret(value: Optional[str]) -> str:
    """展示 Key 时只露前 4 位和后 4 位。界面和日志里任何位置都必须走这里。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"
