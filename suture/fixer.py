"""备份、写入、回滚。

这一层只执行动作，不做判断：写哪个字段、写什么值由检测层给出。
备份文件里同样有 Key 明文，所以权限要收紧。
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .harness.base import HarnessAdapter, HarnessConfig


@dataclass
class BackupEntry:
    original_path: str
    backup_path: str
    existed: bool


@dataclass
class BackupManifest:
    timestamp: str
    entries: List[BackupEntry] = field(default_factory=list)
    # 持久化环境变量的备份：{变量名: 修复前的值}，None 表示修复前压根没设置过这个变量，
    # 回滚时要删掉而不是写回一个空字符串。跟 entries（文件）是平行的两套备份，
    # 因为环境变量不是文件，没法用 shutil.copy2 那一套。
    env_entries: Dict[str, Optional[str]] = field(default_factory=dict)
    directory: str = ""


def backup_root(home: Optional[str] = None) -> str:
    base = home or os.path.expanduser("~")
    return os.path.join(base, ".suture", "backups")


def backup_files(paths: List[str], home: Optional[str] = None) -> BackupManifest:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = os.path.join(backup_root(home), stamp)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(os.path.dirname(directory), 0o700)
        os.chmod(directory, 0o700)
    except OSError:
        pass

    manifest = BackupManifest(timestamp=stamp, directory=directory)
    for i, path in enumerate(paths):
        exists = os.path.exists(path)
        dest = os.path.join(directory, f"{i:02d}-{os.path.basename(path)}")
        if exists:
            shutil.copy2(path, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
        manifest.entries.append(BackupEntry(original_path=path, backup_path=dest, existed=exists))
    return manifest


def rollback(manifest: BackupManifest) -> List[str]:
    """回滚到备份状态。返回回滚失败的文件列表——失败必须如实上报，
    不能让用户以为已经恢复原状。"""
    failed: List[str] = []
    for entry in manifest.entries:
        try:
            if entry.existed:
                os.makedirs(os.path.dirname(entry.original_path), exist_ok=True)
                shutil.copy2(entry.backup_path, entry.original_path)
            elif os.path.exists(entry.original_path):
                os.remove(entry.original_path)      # 修复时新建的文件，回滚就该删掉
        except OSError:
            failed.append(entry.original_path)
    return failed


def apply_fixes(adapter: HarnessAdapter, cfg: HarnessConfig, changes: Dict[str, str],
                env=None, home=None, project_dir=None) -> List[str]:
    return adapter.apply(cfg, changes, env=env, home=home, project_dir=project_dir)


def backup_env(adapter: HarnessAdapter, cfg: HarnessConfig,
               changes: Dict[str, str]) -> Dict[str, Optional[str]]:
    """备份即将被改动的持久化环境变量的当前值。必须在 apply_fixes 之前调用，
    读到的才是「修复前」的值；调用 adapter.env_var_targets 只是预览要改哪些变量，
    这一步本身不写任何东西。"""
    targets = adapter.env_var_targets(cfg, changes)
    return {name: adapter.read_persistent_env(name) for name in targets}


def rollback_env(adapter: HarnessAdapter, env_entries: Dict[str, Optional[str]]) -> List[str]:
    """回滚持久化环境变量。原来没设置过的（None）删掉，原来有值的写回原值。
    返回回滚失败的变量名列表，跟 rollback() 对文件的失败上报是同一个约定。"""
    failed: List[str] = []
    for name, old_value in env_entries.items():
        try:
            if old_value is None:
                adapter.unset_persistent_env(name)
            else:
                adapter.write_persistent_env(name, old_value)
        except (OSError, RuntimeError):
            failed.append(name)
    return failed
