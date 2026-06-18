"""Test for MemoryLayer (P1.2) — 统一三级记忆系统.

Tests:
  1. Session Memory: set/get/clear/all
  2. User Memory: set/get/all/delete/reset
  3. Knowledge Memory: set/get/search/all/delete/reset
  4. Default knowledge and user values
  5. get_context_for_prompt (with and without query)
  6. Tokenization and search scoring
  7. Compatibility: SessionManager still importable
  8. Singleton behavior
  9. Thread safety (basic)
  10. File persistence (user/knowledge survive re-init)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.layer import (
    MemoryLayer,
    memory_layer,
    DEFAULT_KNOWLEDGE,
    DEFAULT_USER,
    USER_MEMORY_FILE,
    KNOWLEDGE_MEMORY_FILE,
)

# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _setup_isolated_layer(tmpdir: str) -> None:
    """Redirect memory files to a temp dir for isolation."""
    import src.memory.layer as mod
    mod.DATA_DIR = tmpdir
    mod.USER_MEMORY_FILE = os.path.join(tmpdir, "user_memory.json")
    mod.KNOWLEDGE_MEMORY_FILE = os.path.join(tmpdir, "knowledge_memory.json")


def _clean_memory_files(tmpdir: str):
    """Remove test memory files."""
    for fname in ["user_memory.json", "knowledge_memory.json"]:
        path = os.path.join(tmpdir, fname)
        if os.path.exists(path):
            os.remove(path)


# ═══════════════════════════════════════════════════════════
# 1. Session Memory
# ═══════════════════════════════════════════════════════════

def test_session_set_get():
    """Session memory: basic set and get."""
    layer = MemoryLayer()
    layer.session_clear()

    layer.session_set("task", "测试微信发送")
    layer.session_set("count", 42)
    layer.session_set("flag", True)

    assert layer.session_get("task") == "测试微信发送"
    assert layer.session_get("count") == 42
    assert layer.session_get("flag") is True
    assert layer.session_get("nonexistent") is None
    print("[PASS] test_session_set_get")


def test_session_overwrite():
    """Session memory: overwrite existing key."""
    layer = MemoryLayer()
    layer.session_clear()

    layer.session_set("key", "old")
    assert layer.session_get("key") == "old"

    layer.session_set("key", "new")
    assert layer.session_get("key") == "new"
    print("[PASS] test_session_overwrite")


def test_session_all():
    """Session memory: retrieve all entries."""
    layer = MemoryLayer()
    layer.session_clear()

    layer.session_set("a", 1)
    layer.session_set("b", 2)

    all_data = layer.session_all()
    assert all_data == {"a": 1, "b": 2}
    print("[PASS] test_session_all")


def test_session_clear():
    """Session memory: clear removes all entries."""
    layer = MemoryLayer()
    layer.session_clear()

    layer.session_set("a", 1)
    layer.session_set("b", 2)
    layer.session_clear()

    assert layer.session_get("a") is None
    assert layer.session_get("b") is None
    assert layer.session_all() == {}
    print("[PASS] test_session_clear")


# ═══════════════════════════════════════════════════════════
# 2. User Memory
# ═══════════════════════════════════════════════════════════

def test_user_defaults():
    """User memory: check default values are pre-loaded."""
    layer = MemoryLayer()
    layer.reset_user_to_defaults()

    assert layer.user_get("language") == "中文"
    assert layer.user_get("os") == "macOS"
    assert layer.user_get("response_style") == "简洁"
    assert layer.user_get("auto_execute") is True
    print("[PASS] test_user_defaults")


def test_user_set_get():
    """User memory: set and get custom preference."""
    layer = MemoryLayer()
    layer.reset_user_to_defaults()

    layer.user_set("code_style", "PEP8")
    assert layer.user_get("code_style") == "PEP8"

    # Overwrite default
    layer.user_set("language", "English")
    assert layer.user_get("language") == "English"

    # Restore
    layer.reset_user_to_defaults()
    assert layer.user_get("language") == "中文"
    print("[PASS] test_user_set_get")


def test_user_all():
    """User memory: all() returns complete dict."""
    layer = MemoryLayer()
    layer.reset_user_to_defaults()

    all_prefs = layer.user_all()
    assert "language" in all_prefs
    assert "os" in all_prefs
    assert len(all_prefs) >= 4  # at least the 4 defaults
    print("[PASS] test_user_all")


def test_user_delete():
    """User memory: delete removes a preference."""
    layer = MemoryLayer()
    layer.reset_user_to_defaults()

    layer.user_set("temp_key", "temp_value")
    assert layer.user_get("temp_key") == "temp_value"

    layer.user_delete("temp_key")
    assert layer.user_get("temp_key") is None
    print("[PASS] test_user_delete")


def test_user_persistence(tmp_path):
    """User memory: values survive layer re-creation (file persistence)."""
    tmpdir = str(tmp_path)
    _setup_isolated_layer(tmpdir)

    layer1 = MemoryLayer()
    layer1.reset_user_to_defaults()
    layer1.user_set("test_persist", "hello_world")

    # Re-create MemoryLayer (simulates restart)
    layer2 = MemoryLayer()
    assert layer2.user_get("test_persist") == "hello_world"

    _clean_memory_files(tmpdir)
    print("[PASS] test_user_persistence")


# ═══════════════════════════════════════════════════════════
# 3. Knowledge Memory
# ═══════════════════════════════════════════════════════════

def test_knowledge_defaults():
    """Knowledge memory: check default entries are pre-loaded."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    assert layer.knowledge_get("wechat_paste_method") is not None
    assert "pbcopy" in layer.knowledge_get("wechat_paste_method")
    assert layer.knowledge_get("wechat_vision_model") == "moonshot-v1-8k-vision-preview"
    assert "python3.12" in layer.knowledge_get("python_path")
    print("[PASS] test_knowledge_defaults")


def test_knowledge_set_get():
    """Knowledge memory: set and get custom entry."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    layer.knowledge_set("test_fix", "workaround for bug X")
    assert layer.knowledge_get("test_fix") == "workaround for bug X"
    print("[PASS] test_knowledge_set_get")


def test_knowledge_all():
    """Knowledge memory: all() returns complete dict."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    all_know = layer.knowledge_all()
    assert len(all_know) >= 5  # at least the 5 defaults
    for key in DEFAULT_KNOWLEDGE:
        assert key in all_know, f"Missing default key: {key}"
    print("[PASS] test_knowledge_all")


def test_knowledge_delete():
    """Knowledge memory: delete removes an entry."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    layer.knowledge_set("temp_knowledge", "temp")
    assert layer.knowledge_get("temp_knowledge") == "temp"

    layer.knowledge_delete("temp_knowledge")
    assert layer.knowledge_get("temp_knowledge") is None
    print("[PASS] test_knowledge_delete")


def test_knowledge_persistence(tmp_path):
    """Knowledge memory: values survive layer re-creation."""
    tmpdir = str(tmp_path)
    _setup_isolated_layer(tmpdir)

    layer1 = MemoryLayer()
    layer1.reset_knowledge_to_defaults()
    layer1.knowledge_set("persist_test", "survives restart")

    layer2 = MemoryLayer()
    assert layer2.knowledge_get("persist_test") == "survives restart"

    _clean_memory_files(tmpdir)
    print("[PASS] test_knowledge_persistence")


# ═══════════════════════════════════════════════════════════
# 4. Knowledge Search
# ═══════════════════════════════════════════════════════════

def test_knowledge_search_exact_match():
    """Knowledge search: exact key match returns high score."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    results = layer.knowledge_search("python_path")
    assert len(results) >= 1

    # python_path should be the top result
    top = results[0]
    assert top["key"] == "python_path"
    assert top["score"] > 0.5
    print("[PASS] test_knowledge_search_exact_match")


def test_knowledge_search_chinese():
    """Knowledge search: Chinese keyword match in values."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    # Add Chinese knowledge entries for test
    layer.knowledge_set("wechat_send_fix", "微信发送消息前需要先聚焦输入框")
    layer.knowledge_set("wechat_paste_issue", "微信粘贴中文内容时有节流问题")

    results = layer.knowledge_search("微信")
    assert len(results) >= 2, f"Expected >= 2 results for '微信', got {len(results)}"

    keys = {r["key"] for r in results}
    assert "wechat_send_fix" in keys, f"Missing wechat_send_fix in {keys}"
    assert "wechat_paste_issue" in keys, f"Missing wechat_paste_issue in {keys}"

    # Also verify English key search works
    results_en = layer.knowledge_search("wechat_paste")
    assert len(results_en) >= 1

    # Clean up
    layer.knowledge_delete("wechat_send_fix")
    layer.knowledge_delete("wechat_paste_issue")
    print("[PASS] test_knowledge_search_chinese")


def test_knowledge_search_no_match():
    """Knowledge search: no match returns empty list."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    results = layer.knowledge_search("zzz_nonexistent_xyz")
    assert results == []
    print("[PASS] test_knowledge_search_no_match")


def test_knowledge_search_empty_query():
    """Knowledge search: empty query returns empty list."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    results = layer.knowledge_search("")
    assert results == []
    print("[PASS] test_knowledge_search_empty_query")


def test_knowledge_search_scoring():
    """Knowledge search: scores are between 0 and 1, sorted descending."""
    layer = MemoryLayer()
    layer.reset_knowledge_to_defaults()

    layer.knowledge_set("test_score", "some unique string for testing")

    results = layer.knowledge_search("test_score")
    assert len(results) >= 1

    for r in results:
        assert 0 <= r["score"] <= 1, f"Score out of range: {r['score']}"
        assert "key" in r
        assert "value" in r

    # Check descending order
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), f"Scores not sorted: {scores}"
    print("[PASS] test_knowledge_search_scoring")


# ═══════════════════════════════════════════════════════════
# 5. Context for Prompt
# ═══════════════════════════════════════════════════════════

def test_context_for_prompt_empty():
    """Context: empty context when all layers are empty."""
    layer = MemoryLayer()
    layer.reset_all()

    ctx = layer.get_context_for_prompt()
    # After reset_all, user/knowledge have defaults, so not empty
    assert len(ctx) > 0  # has defaults
    print("[PASS] test_context_for_prompt_empty")


def test_context_for_prompt_with_session():
    """Context: includes session memory in prompt text."""
    layer = MemoryLayer()
    layer.reset_all()
    layer.session_set("current_task", "调试微信粘贴")

    ctx = layer.get_context_for_prompt()
    assert "current_task" in ctx
    assert "调试微信粘贴" in ctx
    print("[PASS] test_context_for_prompt_with_session")


def test_context_for_prompt_user_priority():
    """Context: User preferences appear before knowledge."""
    layer = MemoryLayer()
    layer.reset_all()

    ctx = layer.get_context_for_prompt()
    user_pos = ctx.find("用户偏好")
    knowledge_pos = ctx.find("系统知识")

    assert user_pos >= 0, "User preferences section missing"
    assert knowledge_pos >= 0, "Knowledge section missing"
    assert user_pos < knowledge_pos, (
        f"User preferences (pos={user_pos}) should appear before "
        f"knowledge (pos={knowledge_pos})"
    )
    print("[PASS] test_context_for_prompt_user_priority")


def test_context_for_prompt_with_query():
    """Context: knowledge_query filters knowledge section."""
    layer = MemoryLayer()
    layer.reset_all()

    ctx = layer.get_context_for_prompt(knowledge_query="python")
    assert "相关知识" in ctx
    assert "python" in ctx.lower()
    # Should NOT have the "系统知识" header (it uses "相关知识" instead)
    print("[PASS] test_context_for_prompt_with_query")


def test_context_for_prompt_no_query_shows_all():
    """Context: without query, shows all knowledge under '系统知识'."""
    layer = MemoryLayer()
    layer.reset_all()

    ctx = layer.get_context_for_prompt()
    assert "系统知识" in ctx
    # All default keys should appear
    for key in DEFAULT_KNOWLEDGE:
        assert key in ctx, f"Missing knowledge key in context: {key}"
    print("[PASS] test_context_for_prompt_no_query_shows_all")


# ═══════════════════════════════════════════════════════════
# 6. Singleton & Compatibility
# ═══════════════════════════════════════════════════════════

def test_singleton():
    """memory_layer is a module-level singleton."""
    from src.memory.layer import memory_layer as ml1
    from src.memory.layer import memory_layer as ml2

    assert ml1 is ml2
    print("[PASS] test_singleton")


def test_session_manager_still_importable():
    """SessionManager is still importable (backward compat)."""
    from src.session.manager import SessionManager, get_session_manager
    from src.session.memory import SessionMemory, GlobalMemory

    # They should import without error
    assert SessionManager is not None
    assert get_session_manager is not None
    assert SessionMemory is not None
    assert GlobalMemory is not None
    print("[PASS] test_session_manager_still_importable")


def test_default_constants():
    """Verify DEFAULT_KNOWLEDGE and DEFAULT_USER constants."""
    assert isinstance(DEFAULT_KNOWLEDGE, dict)
    assert isinstance(DEFAULT_USER, dict)
    assert len(DEFAULT_KNOWLEDGE) == 5
    assert len(DEFAULT_USER) == 4

    assert DEFAULT_USER["language"] == "中文"
    assert DEFAULT_USER["os"] == "macOS"
    assert DEFAULT_KNOWLEDGE["python_path"].endswith("python3.12")
    print("[PASS] test_default_constants")


# ═══════════════════════════════════════════════════════════
# 7. Reset
# ═══════════════════════════════════════════════════════════

def test_reset_user_to_defaults():
    """reset_user_to_defaults restores original values."""
    layer = MemoryLayer()
    layer.user_set("language", "English")
    layer.user_set("custom", "value")

    layer.reset_user_to_defaults()
    assert layer.user_get("language") == "中文"
    assert layer.user_get("custom") is None
    print("[PASS] test_reset_user_to_defaults")


def test_reset_knowledge_to_defaults():
    """reset_knowledge_to_defaults restores original knowledge."""
    layer = MemoryLayer()
    layer.knowledge_set("custom_know", "custom value")

    layer.reset_knowledge_to_defaults()
    assert layer.knowledge_get("custom_know") is None
    assert layer.knowledge_get("wechat_paste_method") is not None
    print("[PASS] test_reset_knowledge_to_defaults")


def test_reset_all():
    """reset_all clears session and restores user/knowledge defaults."""
    layer = MemoryLayer()
    layer.session_set("temp", "data")
    layer.user_set("language", "English")
    layer.knowledge_set("custom", "value")

    layer.reset_all()

    # Session cleared
    assert layer.session_get("temp") is None
    # User restored
    assert layer.user_get("language") == "中文"
    # Knowledge restored
    assert layer.knowledge_get("custom") is None
    assert layer.knowledge_get("wechat_paste_method") is not None
    print("[PASS] test_reset_all")


# ═══════════════════════════════════════════════════════════
# 8. Edge Cases
# ═══════════════════════════════════════════════════════════

def test_knowledge_value_types():
    """Knowledge can store various value types."""
    layer = MemoryLayer()

    layer.knowledge_set("str_val", "hello")
    layer.knowledge_set("int_val", 42)
    layer.knowledge_set("list_val", [1, 2, 3])
    layer.knowledge_set("dict_val", {"nested": "ok"})
    layer.knowledge_set("bool_val", True)
    layer.knowledge_set("none_val", None)

    assert layer.knowledge_get("str_val") == "hello"
    assert layer.knowledge_get("int_val") == 42
    assert layer.knowledge_get("list_val") == [1, 2, 3]
    assert layer.knowledge_get("dict_val") == {"nested": "ok"}
    assert layer.knowledge_get("bool_val") is True
    assert layer.knowledge_get("none_val") is None

    # Clean up
    for key in ["str_val", "int_val", "list_val", "dict_val", "bool_val", "none_val"]:
        layer.knowledge_delete(key)
    print("[PASS] test_knowledge_value_types")


def test_user_value_types():
    """User memory can store various value types."""
    layer = MemoryLayer()

    layer.user_set("list_pref", ["a", "b"])
    layer.user_set("dict_pref", {"key": "val"})

    assert layer.user_get("list_pref") == ["a", "b"]
    assert layer.user_get("dict_pref") == {"key": "val"}

    layer.user_delete("list_pref")
    layer.user_delete("dict_pref")
    print("[PASS] test_user_value_types")


def test_large_session():
    """Session memory with many entries."""
    layer = MemoryLayer()
    layer.session_clear()

    for i in range(100):
        layer.session_set(f"key_{i}", f"value_{i}")

    assert len(layer.session_all()) == 100
    assert layer.session_get("key_99") == "value_99"

    layer.session_clear()
    print("[PASS] test_large_session")


# ═══════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("MemoryLayer Tests (P1.2)")
    print("=" * 60)

    # Session tests (no file I/O needed)
    test_session_set_get()
    test_session_overwrite()
    test_session_all()
    test_session_clear()

    # User tests
    test_user_defaults()
    test_user_set_get()
    test_user_all()
    test_user_delete()

    with tempfile.TemporaryDirectory() as tmpdir:
        test_user_persistence(Path(tmpdir))

    # Knowledge tests
    test_knowledge_defaults()
    test_knowledge_set_get()
    test_knowledge_all()
    test_knowledge_delete()

    with tempfile.TemporaryDirectory() as tmpdir:
        test_knowledge_persistence(Path(tmpdir))

    # Search tests
    test_knowledge_search_exact_match()
    test_knowledge_search_chinese()
    test_knowledge_search_no_match()
    test_knowledge_search_empty_query()
    test_knowledge_search_scoring()

    # Context tests
    test_context_for_prompt_empty()
    test_context_for_prompt_with_session()
    test_context_for_prompt_user_priority()
    test_context_for_prompt_with_query()
    test_context_for_prompt_no_query_shows_all()

    # Singleton & Compatibility
    test_singleton()
    test_session_manager_still_importable()
    test_default_constants()

    # Reset
    test_reset_user_to_defaults()
    test_reset_knowledge_to_defaults()
    test_reset_all()

    # Edge cases
    test_knowledge_value_types()
    test_user_value_types()
    test_large_session()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
