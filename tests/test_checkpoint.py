# -*- coding: utf-8 -*-
"""Checkpoint 错误恢复系统测试。"""

import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.checkpoint.checkpoint import Checkpoint, _sanitize_name, _hash_path


class TestCheckpointBasics:
    """测试 checkpoint 基本功能: 保存 / 回滚 / 列表 / 清理。"""

    @pytest.fixture
    def ck(self):
        """在临时目录中创建 Checkpoint 实例。"""
        tmp = tempfile.mkdtemp(prefix="ck_test_")
        yield Checkpoint(root=tmp)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_sanitize_name(self):
        """名称净化应产生安全目录名。"""
        safe = _sanitize_name("My Feature v2.0!")
        assert " " not in safe
        assert "!" not in safe
        assert "." not in safe
        # 应包含时间戳后缀
        assert "-" in safe

    def test_hash_path_stable(self):
        """路径哈希应稳定。"""
        h1 = _hash_path("src/main.py")
        h2 = _hash_path("src/main.py")
        assert h1 == h2
        assert len(h1) == 12

    def test_save_and_rollback(self, ck):
        """保存文件后修改，回滚应恢复原始内容。"""
        import tempfile as _tmp

        # 创建临时测试文件
        with _tmp.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("print('hello world')\n")
            test_file = f.name

        try:
            # 获取相对于项目的路径(模拟项目文件)
            # 直接使用绝对路径 — Checkpoint 会处理
            orig_content = "print('hello world')\n"

            # Save — 需要让 Checkpoint 能找到文件
            # 将文件放入项目 src 目录(模拟)
            project_src = os.path.join(os.path.dirname(__file__), "..", "src")
            dest = os.path.join(project_src, "_ck_test_temp.py")
            shutil.copy(test_file, dest)
            rel_path = "src/_ck_test_temp.py"

            try:
                ck_id = ck.save("test-save", [rel_path])
                assert ck_id is not None
                assert os.path.isdir(os.path.join(ck.root, ck_id))

                # 修改文件
                with open(dest, "w") as f:
                    f.write("print('modified!')\n")

                # 验证已修改
                with open(dest) as f:
                    assert f.read() == "print('modified!')\n"

                # 回滚
                ok = ck.rollback(ck_id)
                assert ok is True

                # 验证已恢复
                with open(dest) as f:
                    assert f.read() == orig_content
            finally:
                if os.path.exists(dest):
                    os.remove(dest)
        finally:
            os.remove(test_file)

    def test_save_nonexistent_file_raises(self, ck):
        """保存不存在的文件应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            ck.save("bad-save", ["nonexistent/file.py"])

    def test_rollback_nonexistent_returns_false(self, ck):
        """回滚不存在的 checkpoint 应返回 False。"""
        assert ck.rollback("nonexistent-ck-id") is False

    def test_list_empty(self, ck):
        """空 checkpoint 存储应返回空列表。"""
        assert ck.list() == []

    def test_list_after_save(self, ck):
        """保存后 list 应能看到 checkpoint。"""
        import tempfile as _tmp

        # 创建临时文件
        test_file = os.path.join(
            os.path.dirname(__file__), "..", "src", "_ck_list_test.py"
        )
        try:
            with open(test_file, "w") as f:
                f.write("# test\n")

            ck.save("list-test", ["src/_ck_list_test.py"])
            items = ck.list()
            assert len(items) == 1
            assert items[0]["name"] == "list-test"
            assert "src/_ck_list_test.py" in items[0]["files"]
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_cleanup(self, ck):
        """清理应删除超出保留数量的旧 checkpoint。"""
        import tempfile as _tmp

        test_file = os.path.join(
            os.path.dirname(__file__), "..", "src", "_ck_cleanup_test.py"
        )
        try:
            with open(test_file, "w") as f:
                f.write("# test\n")

            # 创建 5 个 checkpoint
            for i in range(5):
                ck.save(f"ck-{i}", ["src/_ck_cleanup_test.py"])

            assert len(ck.list()) == 5

            # 清理，只保留 3 个
            removed = ck.cleanup(keep_last=3)
            assert removed == 2
            assert len(ck.list()) == 3
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_cleanup_no_op(self, ck):
        """当 checkpoint 数量小于保留数时不应清理。"""
        assert ck.cleanup(keep_last=10) == 0

    def test_get_existing(self, ck):
        """get 应返回已存在的 checkpoint 详情。"""
        import tempfile as _tmp

        test_file = os.path.join(
            os.path.dirname(__file__), "..", "src", "_ck_get_test.py"
        )
        try:
            with open(test_file, "w") as f:
                f.write("# get test\n")

            ck_id = ck.save("get-test", ["src/_ck_get_test.py"])
            meta = ck.get(ck_id)
            assert meta is not None
            assert meta["name"] == "get-test"
            assert len(meta["files"]) == 1
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_get_nonexistent_returns_none(self, ck):
        """get 对不存在的 checkpoint 应返回 None。"""
        assert ck.get("nope-12345") is None


class TestCheckpointEdgeCases:
    """边缘情况测试。"""

    @pytest.fixture
    def ck(self):
        tmp = tempfile.mkdtemp(prefix="ck_edge_")
        yield Checkpoint(root=tmp)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_save_multiple_files(self, ck):
        """保存多个文件，回滚全部恢复。"""
        files_data = {
            "src/_ck_multi_a.py": "a = 1\n",
            "src/_ck_multi_b.py": "b = 2\n",
        }

        project_root = os.path.join(os.path.dirname(__file__), "..")
        created = []

        try:
            for rel, content in files_data.items():
                abs_path = os.path.join(project_root, rel)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w") as f:
                    f.write(content)
                created.append(abs_path)

            ck_id = ck.save("multi-test", list(files_data.keys()))

            # 修改所有文件
            for abs_path in created:
                with open(abs_path, "w") as f:
                    f.write("# modified\n")

            # 回滚
            assert ck.rollback(ck_id) is True

            # 验证全部恢复
            for rel, expected in files_data.items():
                abs_path = os.path.join(project_root, rel)
                with open(abs_path) as f:
                    assert f.read() == expected, f"文件 {rel} 未恢复"
        finally:
            for abs_path in created:
                if os.path.exists(abs_path):
                    os.remove(abs_path)

    def test_rollback_missing_backup_file(self, ck):
        """如果备份文件被手动删除，rollback 返回 False(无文件可恢复)。"""
        import tempfile as _tmp

        test_file = os.path.join(
            os.path.dirname(__file__), "..", "src", "_ck_broken_test.py"
        )
        try:
            with open(test_file, "w") as f:
                f.write("# original\n")

            ck_id = ck.save("broken-test", ["src/_ck_broken_test.py"])

            # 删除备份文件
            ck_dir = os.path.join(ck.root, ck_id)
            for entry in os.listdir(ck_dir):
                entry_path = os.path.join(ck_dir, entry)
                if os.path.isfile(entry_path) and entry != "metadata.json":
                    os.remove(entry_path)

            # rollback 返回 False(没有备份文件可恢复)
            result = ck.rollback(ck_id)
            assert result is False
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)


class TestCheckpointSingleton:
    """测试全局单例。"""

    def test_get_checkpoint_returns_same_instance(self):
        from src.checkpoint import get_checkpoint
        ck1 = get_checkpoint()
        ck2 = get_checkpoint()
        assert ck1 is ck2

    def test_get_checkpoint_root_dir(self):
        from src.checkpoint import get_checkpoint
        ck = get_checkpoint()
        assert "checkpoints" in ck.root


class TestGraphIntegration:
    """测试 graph.py 中的 checkpoint 辅助函数。"""

    def test_safe_slug_basic(self):
        from src.ceo.graph import _safe_slug
        assert _safe_slug("Hello World!") == "Hello-World"
        assert _safe_slug("测试-任务") == "测试-任务"

    def test_safe_slug_long_text(self):
        from src.ceo.graph import _safe_slug
        result = _safe_slug("a" * 100, max_len=20)
        assert len(result) <= 20

    def test_safe_slug_empty(self):
        from src.ceo.graph import _safe_slug
        assert _safe_slug("") == "task"
        assert _safe_slug("!!!") == "task"

    def test_discover_project_files(self):
        from src.ceo.graph import _discover_project_files
        files = _discover_project_files()
        assert isinstance(files, list)
        assert len(files) > 0
        # 所有文件应以 src/ 或 tests/ 开头
        for f in files:
            assert f.startswith("src/") or f.startswith("tests/"), \
                f"意外路径: {f}"
        # 检查核心文件存在
        assert "src/main.py" in files
        assert "src/config.py" in files

    def test_discover_project_files_excludes_pycache(self):
        from src.ceo.graph import _discover_project_files
        files = _discover_project_files()
        for f in files:
            assert "__pycache__" not in f
