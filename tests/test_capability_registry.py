"""Test script for CapabilityRegistry (P0.2).

Tests:
  1. Default capability registration (12 defaults)
  2. Register a new capability at runtime
  3. Unregister
  4. Intent resolution for all 11 intents
  5. get_agent_for_capability / get_agent_for_intent
  6. get_tools_for_intent (deduplicated tool set)
  7. export_tools_config compatibility
  8. resolve_tools compatibility shim
"""

import sys
import os

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.capability.registry import (
    Capability,
    CapabilityRegistry,
    DEFAULT_CAPABILITIES,
    INTENT_CAPABILITY_MAP,
    capability_registry,
    resolve_tools,
)


def test_default_registration():
    """All 13 default capabilities should be pre-registered (12 + 1 compat alias)."""
    all_caps = capability_registry.list_all()
    assert len(all_caps) == 13, f"Expected 13 defaults, got {len(all_caps)}"
    names = {c.name for c in all_caps}
    expected = set(DEFAULT_CAPABILITIES.keys())
    assert names == expected, f"Missing: {expected - names}, Extra: {names - expected}"
    print("[PASS] test_default_registration — 13 capabilities registered")


def test_capability_fields():
    """Verify individual capability fields."""
    web = capability_registry.get("web_search")
    assert web is not None
    assert web.agent == "ResearchAgent"
    assert "web_search" in web.tools
    assert "web_fetch" in web.tools
    assert "market_series" in web.tools

    coding = capability_registry.get("coding")
    assert coding.agent == "CodingAgent"
    # patch/terminal 执行层不存在，已从 coding 能力移除
    assert "run_python" in coding.tools
    assert "run_test" in coding.tools
    assert "lint_code" in coding.tools

    vision = capability_registry.get("vision")
    assert vision.agent == "VisionAgent"
    # vision_analyze/screenshot 执行层不存在，已清空 tools（视觉走独立 /vision 子系统）
    assert vision.tools == []

    print("[PASS] test_capability_fields — individual fields verified")


def test_register_new_capability():
    """Register a new capability at runtime (zero code change test)."""
    cap = Capability(
        name="excel",
        description="Excel文件读写和处理",
        agent="ExcelAgent",
        tools=["excel_read", "excel_write", "excel_format"],
        requires=["file_io"],
    )
    capability_registry.register(cap)

    retrieved = capability_registry.get("excel")
    assert retrieved is not None, "New capability not found after register"
    assert retrieved.agent == "ExcelAgent"
    assert retrieved.requires == ["file_io"]
    assert len(capability_registry.list_all()) == 14

    print("[PASS] test_register_new_capability — dynamic registration works")


def test_unregister():
    """Unregister a capability."""
    capability_registry.unregister("excel")
    assert capability_registry.get("excel") is None
    assert len(capability_registry.list_all()) == 13

    # Unregister non-existent (no-op)
    capability_registry.unregister("nonexistent")
    assert len(capability_registry.list_all()) == 13

    print("[PASS] test_unregister — unregister works, no-op for missing")


def test_intent_resolution_all():
    """Resolve all 11 intents and verify correct capabilities."""
    test_cases = {
        "COMMAND": ["shell"],
        "SEARCH": ["web_search"],
        "RESEARCH": ["web_search", "file_io"],
        "VISION": ["vision"],
        "SOCIAL": ["wechat"],  # 微信社交仅发消息，vision 不走执行层工具
        "MEMORY": ["memory"],
        "CODING": ["coding", "filesystem", "web_search"],
        "SYSTEM": ["shell", "filesystem"],
        "FILE": ["file_io"],
        "AUTOMATION": ["shell", "web_search"],
        "GENERAL_CHAT": [],
    }

    for intent, expected_names in test_cases.items():
        caps = capability_registry.resolve(intent)
        cap_names = [c.name for c in caps]
        assert cap_names == expected_names, (
            f"Intent {intent}: expected {expected_names}, got {cap_names}"
        )

    # Case-insensitive
    caps_lower = capability_registry.resolve("search")
    assert [c.name for c in caps_lower] == ["web_search"]

    print("[PASS] test_intent_resolution_all — all 11 intents resolved correctly")


def test_get_agent_for_capability():
    """Capability name → agent name."""
    assert capability_registry.get_agent_for_capability("web_search") == "ResearchAgent"
    assert capability_registry.get_agent_for_capability("coding") == "CodingAgent"
    assert capability_registry.get_agent_for_capability("wechat") == "WechatAgent"
    assert capability_registry.get_agent_for_capability("shell") == "SystemAgent"
    assert capability_registry.get_agent_for_capability("nonexistent") == ""

    print("[PASS] test_get_agent_for_capability — agent lookups correct")


def test_get_agent_for_intent():
    """Intent → primary agent (compatibility adapter)."""
    assert capability_registry.get_agent_for_intent("COMMAND") == "SystemAgent"
    assert capability_registry.get_agent_for_intent("CODING") == "CodingAgent"
    assert capability_registry.get_agent_for_intent("SEARCH") == "ResearchAgent"
    assert capability_registry.get_agent_for_intent("RESEARCH") == "ResearchAgent"
    assert capability_registry.get_agent_for_intent("VISION") == "VisionAgent"
    assert capability_registry.get_agent_for_intent("SOCIAL") == "WechatAgent"
    assert capability_registry.get_agent_for_intent("MEMORY") == "MemoryAgent"
    assert capability_registry.get_agent_for_intent("SYSTEM") == "SystemAgent"
    assert capability_registry.get_agent_for_intent("FILE") == "SystemAgent"
    assert capability_registry.get_agent_for_intent("AUTOMATION") == "SystemAgent"
    assert capability_registry.get_agent_for_intent("GENERAL_CHAT") == ""
    assert capability_registry.get_agent_for_intent("UNKNOWN") == ""

    print("[PASS] test_get_agent_for_intent — compatibility adapter works for all intents")


def test_research_alias():
    """\"research\" capability alias resolves to same tools/agent as \"web_search\"."""
    research_cap = capability_registry.get("research")
    web_cap = capability_registry.get("web_search")
    assert research_cap is not None, "research alias not found"
    assert research_cap.agent == web_cap.agent == "ResearchAgent"
    assert research_cap.tools == web_cap.tools
    # resolve_tools should work with "research"
    tools = resolve_tools(["research", "file_io"])
    assert "web_search" in tools
    assert "web_fetch" in tools
    assert "market_series" in tools
    assert "read_file" in tools
    assert "write_file" in tools

    print("[PASS] test_research_alias — research alias maps to web_search correctly")


def test_get_tools_for_intent():
    """Intent → flat, deduplicated tool set."""
    research_tools = capability_registry.get_tools_for_intent("RESEARCH")
    assert research_tools == {
        "web_search",
        "web_fetch",
        "market_series",
        "weather",  # 天气直连数据源，不走 Tavily
        "read_file",
        "write_file",
    }

    coding_tools = capability_registry.get_tools_for_intent("CODING")
    # coding → read_file/write_file/run_python/run_test/lint_code/git_commit
    # (patch/terminal 执行层不存在，已从 coding 能力移除)
    assert "run_python" in coding_tools
    assert "read_file" in coding_tools
    assert "list_dir" in coding_tools  # filesystem capability
    # web_search and coding share some tools — dedup should handle it

    chat_tools = capability_registry.get_tools_for_intent("GENERAL_CHAT")
    assert chat_tools == set()

    print("[PASS] test_get_tools_for_intent — tools correctly resolved and deduplicated")


def test_export_tools_config():
    """Compatibility export to old ROLE_TOOLS format."""
    config = capability_registry.export_tools_config()
    assert "ResearchAgent" in config
    assert "CodingAgent" in config
    assert "WechatAgent" in config
    assert "SystemAgent" in config
    assert "VisionAgent" in config

    # ResearchAgent gets tools from web_search + market_data
    assert "web_search" in config["ResearchAgent"]
    assert "market_series" in config["ResearchAgent"]

    # CodingAgent gets tools from coding + vcs
    assert "git_commit" in config["CodingAgent"]

    print("[PASS] test_export_tools_config — compatible with ROLE_TOOLS format")


def test_resolve_tools_compat():
    """resolve_tools shim works with capability names."""
    tools = resolve_tools(["coding", "vcs"])
    assert "read_file" in tools
    assert "write_file" in tools
    assert "run_python" in tools
    assert "git_commit" in tools
    # No duplicates
    assert len(tools) == len(set(tools))

    # Empty list
    assert resolve_tools([]) == []

    # Unknown capability
    assert resolve_tools(["nonexistent"]) == []

    print("[PASS] test_resolve_tools_compat — compatibility shim works")


def test_capability_dataclass_methods():
    """Test Capability dataclass equality, hash, to_dict, matches_keyword."""
    a = Capability(name="test", description="A test capability")
    b = Capability(name="test", description="Different desc")
    c = Capability(name="other")

    assert a == b  # equality by name
    assert a != c
    assert hash(a) == hash(b)

    d = a.to_dict()
    assert d["name"] == "test"
    assert d["description"] == "A test capability"
    assert "tools" in d

    assert a.matches_keyword("TEST")
    assert a.matches_keyword("capability")
    assert not a.matches_keyword("zzz")

    print("[PASS] test_capability_dataclass_methods — dataclass methods work")


def test_set_intent_capabilities():
    """Dynamic intent remapping."""
    # Save original
    orig = capability_registry.get_intent_capability_names("COMMAND")

    # Remap
    capability_registry.set_intent_capabilities("COMMAND", ["shell", "file_io"])
    caps = capability_registry.resolve("COMMAND")
    assert len(caps) == 2
    assert {c.name for c in caps} == {"shell", "file_io"}

    # Restore
    capability_registry.set_intent_capabilities("COMMAND", orig)
    caps = capability_registry.resolve("COMMAND")
    assert [c.name for c in caps] == ["shell"]

    print("[PASS] test_set_intent_capabilities — dynamic intent remapping works")


def test_register_non_capability_raises():
    """Registering a non-Capability raises ValueError."""
    try:
        capability_registry.register("not_a_capability")  # type: ignore
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Expected Capability instance" in str(e)

    print("[PASS] test_register_non_capability_raises — type check enforced")


def test_singleton_consistency():
    """Module-level singleton and get_capability_registry return same instance."""
    from src.capability.registry import get_capability_registry
    reg1 = capability_registry
    reg2 = get_capability_registry()
    assert reg1 is reg2
    # Register through one, visible through other
    reg1.register(Capability(name="_singleton_test", description="test"))
    assert reg2.get("_singleton_test") is not None
    reg1.unregister("_singleton_test")

    print("[PASS] test_singleton_consistency — singleton works")


# ═══════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_default_registration,
        test_capability_fields,
        test_register_new_capability,
        test_unregister,
        test_intent_resolution_all,
        test_get_agent_for_capability,
        test_get_agent_for_intent,
        test_research_alias,
        test_get_tools_for_intent,
        test_export_tools_config,
        test_resolve_tools_compat,
        test_capability_dataclass_methods,
        test_set_intent_capabilities,
        test_register_non_capability_raises,
        test_singleton_consistency,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__} — {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(0 if failed == 0 else 1)
