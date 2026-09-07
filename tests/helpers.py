"""测试公用的脚手架：隔离的 HOME 和项目目录、指向假网关的规则文件。"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mock_gateway import base_url_for, start_server  # noqa: E402


def real_profile() -> dict:
    with open(os.path.join(ROOT, "gateway_profile.json"), encoding="utf-8") as f:
        return json.load(f)


def profile_for(server, tmpdir: str, **overrides) -> str:
    """把真实规则里的网关地址换成假网关的地址，其余保持一致。"""
    p = real_profile()
    p["base_url"]["canonical_root"] = base_url_for(server)
    p["base_url"]["require_https"] = False
    p.update(overrides)
    path = os.path.join(tmpdir, "profile.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
    return path


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def subprocess_env(**extra) -> dict:
    """给子进程验收测试用的隔离环境：故意只保留验证所需的变量，证明 CLI 真的只从
    --home/--profile 这些参数读配置，不依赖本机其它环境变量。但完全清空环境在
    Windows 上会连 Winsock 都初始化不了（缺 SystemRoot 时创建 socket 直接报
    WinError 10106，跟测试本身想验证的东西无关），所以要把这几个系统级变量原样
    带过去；非 Windows 平台上带 TMPDIR 是同样的道理，避免临时目录解析走到意外的
    兜底路径。"""
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32":
        carry = ("SystemRoot", "SystemDrive", "windir", "ComSpec", "TEMP", "TMP")
    else:
        carry = ("TMPDIR",)
    for name in carry:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(extra)
    return env


class Sandbox:
    """一次测试用的隔离环境：独立的 HOME、项目目录和假网关。"""

    def __init__(self, behavior: str = "ok"):
        self.behavior = behavior

    def __enter__(self):
        self._home = tempfile.TemporaryDirectory()
        self._proj = tempfile.TemporaryDirectory()
        self._cfg = tempfile.TemporaryDirectory()
        self.server = start_server(default_behavior=self.behavior)
        self.home = self._home.name
        self.project = self._proj.name
        self.profile_path = profile_for(self.server, self._cfg.name)
        self.base_url = base_url_for(self.server)
        self.env = {"HOME": self.home, "PATH": os.environ.get("PATH", "")}
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        for d in (self._home, self._proj, self._cfg):
            d.cleanup()
        return False
