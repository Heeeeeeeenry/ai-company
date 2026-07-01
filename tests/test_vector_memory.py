"""测试向量记忆存储 — Tfidf + cosine 语义检索

运行: python -m pytest tests/test_vector_memory.py -v
"""

import os
import sys
import json
import tempfile
import pytest

# Ensure project root in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.memory.vector_store import VectorMemoryStore


# ═══ Fixtures ═══


@pytest.fixture
def empty_store():
    """Empty vector store with temp file."""
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        path = f.name
    store = VectorMemoryStore(index_path=path)
    yield store
    # Cleanup
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def populated_store(empty_store):
    """Store with Chinese memory entries."""
    entries = [
        "用户偏好简洁回复，不喜欢冗长的解释",
        "修改代码后必须端到端测试验证才能说完成",
        "禁止主动删数据，需删除时中文二次确认",
        "WeChat发送是canary功能，坏了用户立刻发现",
        "偏好快速响应：综合查询时并行发起操作",
        "微信联系人: 小号/文件传输助手(默认)",
        "OS: macOS iTerm2透明85% retina 2880x1800",
        "项目: ai-company 狗蛋儿 独立进程个人助手",
        "push规则: 晚上7点左右的push直接执行",
        "语言偏好: 简体中文",
    ]
    for text in entries:
        empty_store.add(text, {"source": "test"})
    return empty_store


# ═══ Tests: Init & Persistence ═══


def test_init_empty_store(empty_store):
    """Empty store should have no entries."""
    assert len(empty_store) == 0
    assert empty_store.is_empty
    assert empty_store.search("测试") == []


def test_add_single_entry(empty_store):
    """Add one entry and verify."""
    eid = empty_store.add("测试记忆条目", {"category": "test"})
    assert eid
    assert len(empty_store) == 1
    assert not empty_store.is_empty


def test_add_duplicate_skips(empty_store):
    """Adding duplicate text should not create new entry."""
    eid1 = empty_store.add("重复条目")
    eid2 = empty_store.add("重复条目")
    assert eid1 == eid2
    assert len(empty_store) == 1


def test_add_empty_raises(empty_store):
    """Empty text should raise ValueError."""
    with pytest.raises(ValueError):
        empty_store.add("")


def test_add_whitespace_only_raises(empty_store):
    """Whitespace-only text should raise ValueError."""
    with pytest.raises(ValueError):
        empty_store.add("   ")


# ═══ Tests: Search ═══


def test_search_exact_match(populated_store):
    """Search for exact text returns it with high score."""
    results = populated_store.search("用户偏好简洁回复")
    assert len(results) > 0
    assert results[0]["score"] > 0.5
    assert "简洁回复" in results[0]["text"]


def test_search_semantic_chinese(populated_store):
    """Semantic search: query about '代码质量' should match relevant entries."""
    results = populated_store.search("代码质量")
    assert len(results) > 0
    # Should find the "端到端测试" entry
    texts = [r["text"] for r in results]
    assert any("测试" in t for t in texts)


def test_search_semantic_wechat(populated_store):
    """Semantic search: '微信发送' should match WeChat entries."""
    results = populated_store.search("微信发送")
    assert len(results) > 0
    texts = [r["text"] for r in results]
    assert any("WeChat" in t or "微信" in t for t in texts)


def test_search_non_matching(empty_store):
    """Search on empty store returns empty."""
    empty_store.add("无关条目")
    results = empty_store.search("完全不匹配的查询XYZABC")
    # May return low-score results or empty
    if results:
        assert results[0]["score"] < 0.3


def test_search_top_k(populated_store):
    """Verify top_k limits results."""
    results = populated_store.search("偏好", top_k=3)
    assert len(results) <= 3


def test_search_all_scores_descending(populated_store):
    """Results should be sorted by score descending."""
    results = populated_store.search("规则")
    if len(results) > 1:
        for i in range(len(results) - 1):
            assert results[i]["score"] >= results[i + 1]["score"]


# ═══ Tests: Build Index ═══


def test_build_index_overwrites(populated_store):
    """build_index should replace all entries."""
    new_texts = ["全新条目A", "全新条目B"]
    populated_store.build_index(new_texts)
    assert len(populated_store) == 2
    results = populated_store.search("全新条目")
    assert len(results) == 2


# ═══ Tests: Remove ═══


def test_remove_by_id(populated_store):
    """Remove entry by ID."""
    # First find an entry ID
    results = populated_store.search("macOS")
    assert len(results) > 0
    eid = results[0]["id"]
    removed = populated_store.remove(eid)
    assert removed is True
    # Verify the specific text is gone
    results2 = populated_store.search("macOS")
    texts = [r["text"] for r in results2]
    assert "OS: macOS iTerm2透明85% retina 2880x1800" not in texts


def test_remove_nonexistent(empty_store):
    """Removing non-existent ID returns False."""
    assert empty_store.remove("nonexistent_id") is False


def test_remove_by_text(populated_store):
    """remove_by_text with substring."""
    count = populated_store.remove_by_text("微信")
    assert count >= 1
    results = populated_store.search("微信")
    assert len(results) == 0


# ═══ Tests: Persistence ═══


def test_persistence_roundtrip():
    """Save and reload store."""
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
        path = f.name

    try:
        # Create and populate
        store1 = VectorMemoryStore(index_path=path)
        store1.add("持久化测试条目", {"test": True})
        store1.add("第二个条目")

        # Reload
        store2 = VectorMemoryStore(index_path=path)
        assert len(store2) == 2
        results = store2.search("持久化")
        assert len(results) > 0
        assert "持久化测试条目" in results[0]["text"]
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ═══ Tests: Edge Cases ═══


def test_search_short_query(populated_store):
    """Very short query (1-2 chars) should still work."""
    results = populated_store.search("OS")
    assert isinstance(results, list)


def test_search_special_chars(populated_store):
    """Query with special characters."""
    results = populated_store.search("85%")
    assert isinstance(results, list)
    # Should match the macOS entry with "85%"
    if results:
        texts = [r["text"] for r in results]
        assert any("85%" in t for t in texts)


# ═══ Integration: EngramBackend ═══


def test_engram_semantic_search():
    """Test semantic_search through EngramBackend."""
    from src.memory.hermes import hermes_memory

    hm = hermes_memory
    hm.add("测试向量搜索: 这是一条关于Python编程的记忆", category="test")

    results = hm.semantic_search("编程语言", top_k=3)
    assert isinstance(results, list)

    hm.remove("测试向量搜索")


def test_engram_search():
    """Test that engram search returns results with score fields."""
    from src.memory.hermes import hermes_memory

    hm = hermes_memory
    hm.add("测试搜索: Python异步编程最佳实践", category="test")

    results = hm.search("Python")
    assert isinstance(results, list)

    hm.remove("测试搜索")
