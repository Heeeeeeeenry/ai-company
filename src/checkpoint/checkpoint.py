# -*- coding: utf-8 -*-
"""Checkpoint — 文件系统级别的执行前状态保存和失败后回滚。

存储路径: ~/.ai-company/checkpoints/<checkpoint_id>/
每个 checkpoint 目录包含:
  - metadata.json  (名称、时间、文件列表)
  - <file_path_hash>/ (原始文件的备份副本)

用法:
    ck = Checkpoint()
    ck.save("before-refactor", ["src/main.py", "src/config.py"])
    # ... 执行任务 ...
    if failed:
        ck.rollback("before-refactor")
    ck.cleanup(keep_last=5)
"""

import json
import os
import shutil
import time
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# 项目根目录 — 用于解析相对路径
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

# Checkpoint 存储根目录
_CHECKPOINT_ROOT = os.path.join(
    os.path.expanduser("~"), ".ai-company", "checkpoints"
)


def _sanitize_name(name: str) -> str:
    """将名称转为安全的目录名(只保留字母数字、中文、连字符、下划线)。"""
    # 替换空白为 -
    safe = re.sub(r'\s+', '-', name.strip())
    # 去掉危险字符
    safe = re.sub(r'[^\w\u4e00-\u9fff\-]', '', safe)
    # 限制长度
    if len(safe) > 64:
        safe = safe[:64]
    # 添加时间戳后缀保证唯一性
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{safe}-{ts}"


def _hash_path(path: str) -> str:
    """为文件路径生成短哈希，用作备份文件名。"""
    return hashlib.md5(path.encode()).hexdigest()[:12]


class Checkpoint:
    """文件系统 checkpoint — 备份/回滚/列表/清理。"""

    def __init__(self, root: Optional[str] = None):
        self.root = root or _CHECKPOINT_ROOT

    @property
    def _root_path(self) -> Path:
        return Path(self.root)

    def _checkpoint_dir(self, name: str) -> Path:
        return self._root_path / name

    # ── save ──────────────────────────────────────

    def save(self, name: str, files: list[str]) -> str:
        """保存 checkpoint.

        Args:
            name: checkpoint 的名称(会自动净化并添加时间戳)
            files: 要备份的文件列表(相对于项目根目录的路径)

        Returns:
            实际的 checkpoint ID (目录名)

        Raises:
            FileNotFoundError: 如果有文件不存在
        """
        safe_name = _sanitize_name(name)
        ck_dir = self._checkpoint_dir(safe_name)
        ck_dir.mkdir(parents=True, exist_ok=True)

        saved_files = []
        for fpath in files:
            # 解析为绝对路径
            abs_path = os.path.join(_PROJECT_ROOT, fpath)
            if not os.path.isfile(abs_path):
                raise FileNotFoundError(f"文件不存在: {abs_path}")

            hash_name = _hash_path(fpath)
            backup_path = ck_dir / hash_name
            shutil.copy2(abs_path, backup_path)
            saved_files.append({
                "path": fpath,
                "abs_path": abs_path,
                "backup": str(backup_path),
                "size": os.path.getsize(abs_path),
            })

        # 写 metadata
        metadata = {
            "name": name,
            "id": safe_name,
            "created": datetime.now().isoformat(),
            "created_ts": time.time(),
            "files": saved_files,
        }
        meta_path = ck_dir / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        return safe_name

    # ── rollback ──────────────────────────────────

    def rollback(self, name: str) -> bool:
        """回滚到指定 checkpoint.

        将备份的文件复制回原始位置。

        Args:
            name: checkpoint ID (完整目录名)

        Returns:
            True 表示回滚成功
        """
        ck_dir = self._checkpoint_dir(name)
        if not ck_dir.is_dir():
            return False

        meta_path = ck_dir / "metadata.json"
        if not meta_path.is_file():
            return False

        with open(meta_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        rolled_back = 0
        for finfo in metadata.get("files", []):
            backup_path = Path(finfo["backup"])
            target_path = Path(finfo["abs_path"])

            if not backup_path.is_file():
                continue

            # 确保目标目录存在
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(backup_path), str(target_path))
            rolled_back += 1

        return rolled_back > 0

    # ── list ──────────────────────────────────────

    def list(self) -> list[dict]:
        """列出所有 checkpoint。

        Returns:
            checkpoint 列表，按创建时间降序排列。每项包含:
              - id: checkpoint ID
              - name: 原始名称
              - created: 创建时间(ISO格式)
              - files: 备份的文件列表
        """
        self._root_path.mkdir(parents=True, exist_ok=True)

        result = []
        for entry in sorted(
            self._root_path.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True
        ):
            if not entry.is_dir():
                continue
            meta_path = entry / "metadata.json"
            if not meta_path.is_file():
                continue

            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                result.append({
                    "id": entry.name,
                    "name": metadata.get("name", entry.name),
                    "created": metadata.get("created", ""),
                    "files": [f["path"] for f in metadata.get("files", [])],
                })
            except (json.JSONDecodeError, KeyError):
                continue

        return result

    # ── cleanup ───────────────────────────────────

    def cleanup(self, keep_last: int = 10) -> int:
        """清理旧 checkpoint，只保留最近 N 个。

        Args:
            keep_last: 保留的数量(默认 10)

        Returns:
            删除的 checkpoint 数量
        """
        all_ck = self.list()
        if len(all_ck) <= keep_last:
            return 0

        to_remove = all_ck[keep_last:]
        removed = 0
        for ck in to_remove:
            ck_dir = self._checkpoint_dir(ck["id"])
            if ck_dir.is_dir():
                shutil.rmtree(str(ck_dir), ignore_errors=True)
                removed += 1

        return removed

    # ── get ───────────────────────────────────────

    def get(self, name: str) -> Optional[dict]:
        """获取单个 checkpoint 的详细信息。

        Args:
            name: checkpoint ID

        Returns:
            checkpoint 元数据，不存在返回 None
        """
        ck_dir = self._checkpoint_dir(name)
        meta_path = ck_dir / "metadata.json"
        if not meta_path.is_file():
            return None

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None


# ── 全局单例 ──────────────────────────────────────

_checkpoint: Optional[Checkpoint] = None


def get_checkpoint() -> Checkpoint:
    """获取全局 Checkpoint 单例。"""
    global _checkpoint
    if _checkpoint is None:
        _checkpoint = Checkpoint()
    return _checkpoint
