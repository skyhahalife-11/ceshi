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
