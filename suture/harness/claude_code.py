"""Claude Code CLI 适配器。

配置载体：环境变量 + 全局 ~/.claude/settings.json + 项目级 <项目>/.claude/settings.json
优先级：环境变量 > 项目级配置 > 全局配置
鉴权：值直接存在配置里；填在 ANTHROPIC_API_KEY 走 x-api-key 头，
      填在 ANTHROPIC_AUTH_TOKEN 走 Authorization 头。
      另外 ANTHROPIC_CUSTOM_HEADERS（`Name: Value`，多头换行分隔）是平行的第三条通路：
      只要里面的头名是网关认的那几个（比如 Token），就跟主字段一起被当成鉴权来源，
      客户端会把两边都发出去，不能只认 API_KEY/AUTH_TOKEN 而把这条路当不存在。
网关地址：不带 /v1，客户端自己会在后面拼 /v1/messages。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    ExtraAuthHeader, FIELD_AUTH, FIELD_BASE_URL, FIELD_MODEL, FileState,
    HarnessAdapter, HarnessConfig, LayerValue, build_resolved, resolve_home,
    resolve_project_dir,
)

ENV_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_MODEL = "ANTHROPIC_MODEL"
ENV_CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"
RELEVANT_ENV = [ENV_BASE_URL, ENV_API_KEY, ENV_AUTH_TOKEN, ENV_MODEL, ENV_CUSTOM_HEADERS]

ENV_LABELS = {ENV_BASE_URL: "网关地址", ENV_MODEL: "模型名称", ENV_API_KEY: "鉴权信息"}


# ---- 持久化环境变量（Windows 注册表 HKCU\Environment）----
# 这一层跟 os.environ 是两件不同的事：os.environ 是进程启动时的快照，改注册表
# 不会让已经在运行的进程感知到；这里的三个函数专门操作持久化存储本身，
# 不经过 os.environ，这样「验证刚写的值是否生效」才能拿到真实的最新值。

def _env_registry_root():
    import winreg  # noqa: PLC0415 —— 只有 Windows 有这个模块，延迟导入
    return winreg.HKEY_CURRENT_USER, "Environment"


def _broadcast_env_change() -> None:
    """写完注册表后广播一下，让 Explorer 之后派生的新进程能感知到；
    已经在运行的终端/程序感知不到，这个广播解决不了那部分，只能提示用户重开。"""
    try:
        import ctypes  # noqa: PLC0415
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, None)
    except Exception:
        pass


def _read_persistent_env(name: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    import winreg  # noqa: PLC0415
    try:
        hive, sub = _env_registry_root()
        with winreg.OpenKey(hive, sub) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except (FileNotFoundError, OSError):
        return None


def _write_persistent_env(name: str, value: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("持久化环境变量目前只实现了 Windows（注册表）这一种。")
    import winreg  # noqa: PLC0415
    hive, sub = _env_registry_root()
    with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _broadcast_env_change()


def _unset_persistent_env(name: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("持久化环境变量目前只实现了 Windows（注册表）这一种。")
    import winreg  # noqa: PLC0415
    hive, sub = _env_registry_root()
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except (FileNotFoundError, OSError):
        pass
    _broadcast_env_change()


def _parse_custom_headers(raw: str) -> List[Tuple[str, str]]:
    """官方格式：`Name: Value`，多个头用换行分隔（已核实，v2.1.227+）。
    格式不对的行直接跳过，不强行猜——总比塞一个错的进去安全。"""
    out: List[Tuple[str, str]] = []
    for line in raw.split("\n"):
        line = line.strip("\r").strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if name and value:
            out.append((name, value))
    return out


def _credentials_path(home: str) -> str:
    return os.path.join(home, ".claude", ".credentials.json")


def _has_native_login(home: str) -> bool:
    """检测本机是不是已经有一份 Claude 订阅的原生 OAuth 登录会话
    （`claude login` 生成，跟 ANTHROPIC_API_KEY/BASE_URL 这套网关配置无关）。
    只看 claudeAiOauth.accessToken 在不在，不解析/使用里面的令牌——
    这里只是拿来判断「有没有一条完全不经过网关配置的可用连接方式」。"""
    try:
        with open(_credentials_path(home), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return isinstance(oauth, dict) and bool(oauth.get("accessToken"))


def _global_path(home: str) -> str:
    return os.path.join(home, ".claude", "settings.json")


def _project_path(project_dir: str) -> str:
    return os.path.join(project_dir, ".claude", "settings.json")


def _read_json_file(layer: str, path: str, known_keys: List[str]) -> FileState:
    st = FileState(layer=layer, path=path, exists=os.path.exists(path))
    if not st.exists:
        return st
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        st.data = json.loads(raw)
        if not isinstance(st.data, dict):
            st.parse_ok = False
            st.parse_error = "配置文件的最外层应该是一个对象（大括号包起来的结构）"
            st.data = {}
    except json.JSONDecodeError as exc:
        st.parse_ok = False
        st.parse_error = (
            f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}。"
            "格式错误会导致整个文件解析失败，这一层的配置全部不生效，不只是出错的那一处。"
        )
        st.data = {}
    except OSError as exc:
        st.parse_ok = False
        st.parse_error = f"读取失败：{exc}"
    if st.parse_ok and known_keys:
        st.unknown_keys = [k for k in st.data.keys() if k not in known_keys]
    return st


class ClaudeCodeAdapter(HarnessAdapter):
    harness_id = "claude_code"
    display_name = "Claude Code CLI"
    config_format = "json"

    def detect(self, env=None, home=None, project_dir=None) -> bool:
        env = env if env is not None else os.environ
        home = resolve_home(home, env)
        project_dir = resolve_project_dir(project_dir)
        if any(env.get(k) for k in RELEVANT_ENV):
            return True
        if os.path.isdir(os.path.join(home, ".claude")):
            return True
        return os.path.exists(_project_path(project_dir))

    def read(self, env=None, home=None, project_dir=None, known_keys=None,
              accepted_headers=None) -> HarnessConfig:
        env = env if env is not None else os.environ
        home = resolve_home(home, env)
        project_dir = resolve_project_dir(project_dir)
        known_keys = known_keys or []
        accepted_headers = accepted_headers or []
        accepted_lower = {h.lower() for h in accepted_headers}

        g = _read_json_file("全局配置", _global_path(home), known_keys)
        p = _read_json_file("项目级配置", _project_path(project_dir), known_keys)
        cfg = HarnessConfig(harness_id=self.harness_id, display_name=self.display_name,
                            files=[g, p])

        def env_block(fs: FileState) -> Dict[str, Any]:
            block = fs.data.get("env")
            return block if isinstance(block, dict) else {}

        def layers_for(var: str) -> List[LayerValue]:
            # 优先级从高到低：环境变量 > 项目级 > 全局
            out = []
            if env.get(var):
                out.append(LayerValue("环境变量", "", str(env[var])))
            for fs in (p, g):
                v = env_block(fs).get(var)
                if v:
                    out.append(LayerValue(fs.layer, fs.path, str(v)))
            return out

        cfg.fields[FIELD_BASE_URL] = build_resolved(FIELD_BASE_URL, layers_for(ENV_BASE_URL))
        cfg.fields[FIELD_MODEL] = build_resolved(FIELD_MODEL, layers_for(ENV_MODEL))

        api_key = build_resolved("api_key", layers_for(ENV_API_KEY))
        auth_token = build_resolved("auth_token", layers_for(ENV_AUTH_TOKEN))

        # 客户端实际发哪个请求头，取决于用户设置的是哪个变量，
        # 而不是网关“希望”收到哪个头。两个都设置时 API_KEY 优先。
        if api_key.is_set:
            auth = api_key
            cfg.auth_header = "x-api-key"
        elif auth_token.is_set:
            auth = auth_token
            cfg.auth_header = "Authorization"
        else:
            auth = api_key
            cfg.auth_header = "x-api-key"
        auth.key = FIELD_AUTH
        cfg.fields[FIELD_AUTH] = auth

        if api_key.is_set and auth_token.is_set:
            cfg.auth_conflict = (
                f"{ENV_API_KEY} 和 {ENV_AUTH_TOKEN} 两处都填了鉴权信息，"
                f"实际生效的是 {ENV_API_KEY}（走 x-api-key 请求头）。"
            )

        # ANTHROPIC_CUSTOM_HEADERS 是跟 API_KEY/AUTH_TOKEN 平行的另一条鉴权通路：
        # 客户端会把这里面的头原样跟着请求一起发出去，不会因为设置了它就不发
        # x-api-key/Authorization。只挑出网关认的那几个头名，避免把无关的自定义头
        # 误判成鉴权信息。
        custom_headers = build_resolved("custom_headers", layers_for(ENV_CUSTOM_HEADERS))
        if custom_headers.is_set:
            for name, value in _parse_custom_headers(custom_headers.value):
                if name.lower() in accepted_lower:
                    cfg.extra_auth_headers.append(ExtraAuthHeader(
                        header=name, value=value,
                        source=f"{ENV_CUSTOM_HEADERS}（{custom_headers.source_layer}）"))

        cfg.native_login = _has_native_login(home)
        if cfg.native_login and not cfg.fields[FIELD_BASE_URL].is_set and not cfg.fields[FIELD_AUTH].is_set:
            cfg.notes.append(
                "检测到直连网关，没有配置网关地址/鉴权信息，这是正常状态，不需要额外配置"
                "——下面几项检查会按这个状态处理。")

        return cfg

    def writable_paths(self, cfg: HarnessConfig) -> List[str]:
        return [fs.path for fs in cfg.files]

    # ---- 持久化环境变量：只有这个字段「当前实际生效层就是环境变量」时才走这条路，
    # 否则维持原来写 settings.json 的行为。上面 read() 里已经把每个字段解析到
    # 底是哪一层生效（rf.source_layer）算清楚了，这里直接复用，不用重新判断一遍。
    FIELD_TO_ENV = {FIELD_BASE_URL: ENV_BASE_URL, FIELD_MODEL: ENV_MODEL, FIELD_AUTH: ENV_API_KEY}

    def env_var_targets(self, cfg: HarnessConfig, changes: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for logical, value in changes.items():
            env_name = self.FIELD_TO_ENV.get(logical)
            if not env_name:
                continue
            rf = cfg.fields.get(logical)
            if rf is not None and rf.source_layer == "环境变量":
                out[env_name] = value
        return out

    def read_persistent_env(self, name: str) -> Optional[str]:
        return _read_persistent_env(name)

    def write_persistent_env(self, name: str, value: str) -> None:
        _write_persistent_env(name, value)

    def unset_persistent_env(self, name: str) -> None:
        _unset_persistent_env(name)

    def _target_file(self, cfg: HarnessConfig) -> FileState:
        """写到实际生效的那一层。按已确认的使用规范，项目级不应该覆盖全局，
        所以统一写回全局配置；只有当项目级已经存在配置时才写项目级，
        避免写了一层不生效的。"""
        project = next((f for f in cfg.files if f.layer == "项目级配置"), None)
        if project is not None and project.exists and project.parse_ok:
            return project
        return next(f for f in cfg.files if f.layer == "全局配置")

    def apply(self, cfg: HarnessConfig, changes: Dict[str, str],
              env=None, home=None, project_dir=None) -> List[str]:
        # 先分流：这次要改的字段里，哪些当前是被环境变量顶着生效的，就该改环境变量
        # 本身，不能照旧写进 settings.json——写了也不会生效，因为环境变量优先级更高。
        env_targets = self.env_var_targets(cfg, changes)
        env_fields = {logical for logical in changes if self.FIELD_TO_ENV.get(logical) in env_targets}
        file_changes = {k: v for k, v in changes.items() if k not in env_fields}

        described: List[str] = []
        for name, value in env_targets.items():
            self.write_persistent_env(name, value)
            described.append(
                f"{ENV_LABELS.get(name, name)} → {value}（当前生效层是环境变量，"
                f"已写入持久化的环境变量 {name}；已经打开的终端/客户端感知不到，"
                "需要重新打开才会生效）")

        if file_changes:
            target = self._target_file(cfg)
            data = dict(target.data) if target.parse_ok else {}
            block = dict(data.get("env") or {}) if isinstance(data.get("env"), dict) else {}

            for logical, value in file_changes.items():
                if logical == FIELD_BASE_URL:
                    block[ENV_BASE_URL] = value
                    described.append(f"网关地址 → {value}（写入{target.layer}）")
                elif logical == FIELD_MODEL:
                    block[ENV_MODEL] = value
                    described.append(f"模型名称 → {value}（写入{target.layer}）")
                elif logical == FIELD_AUTH:
                    block[ENV_API_KEY] = value
                    block.pop(ENV_AUTH_TOKEN, None)
                    described.append(f"鉴权信息 → 统一填到 {ENV_API_KEY}（写入{target.layer}）")

            data["env"] = block
            os.makedirs(os.path.dirname(target.path), exist_ok=True)
            with open(target.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
        return described

    def generate_minimal_config(self, base_url: str, model: str,
                                env=None, home=None, project_dir=None) -> str:
        env = env if env is not None else os.environ
        home = resolve_home(home, env)
        path = _global_path(home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "env": {
                ENV_BASE_URL: base_url,
                ENV_API_KEY: "把这里换成网关后台生成的 Key",
                ENV_MODEL: model,
            }
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return path
