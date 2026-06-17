"""WeChat flow tests: routing, capability planning, and tool normalization."""

import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_state(user_request: str):
    return {
        "messages": [],
        "user_request": user_request,
        "phase": "triage",
        "department": "",
        "plan": None,
        "research_results": None,
        "execution_log": [],
        "score_card": None,
        "final_output": None,
        "error": None,
        "retry_count": 0,
        "pmo_result": None,
        "retry_feedback": None,
        "prd": None,
        "arch_design": None,
    }


def test_extract_wechat_send_request_supports_natural_language():
    from src.ceo.graph import _extract_wechat_send_request

    parsed = _extract_wechat_send_request(
        "发送消息给微信联系人，小媛儿宝儿，发送消息：多喝水。"
    )
    assert parsed == ("小媛儿宝儿", "多喝水")


def test_extract_wechat_send_request_supports_quoted_form():
    from src.ceo.graph import _extract_wechat_send_request

    parsed = _extract_wechat_send_request(
        '微信给 "小媛儿宝儿" 发送消息 "多喝水"'
    )
    assert parsed == ("小媛儿宝儿", "多喝水")


def test_capability_planner_routes_wechat_send_to_messaging():
    from src.capability.planner import CapabilityPlanner

    plan = CapabilityPlanner().analyze("给微信联系人小媛儿宝儿发送消息：多喝水")
    assert plan.role_hint == "devops"
    assert plan.capabilities == ["messaging"]


def test_wechat_send_json_failure_is_marked_unsuccessful():
    from src.execution.executor import ExecutionRouter, ToolResult, ExecutionMode

    result = ToolResult(
        success=True,
        output='{"success": false, "state": "FIND_CONTACT", "error": "Contact not found"}',
        mode=ExecutionMode.CLI,
    )

    normalized = ExecutionRouter._normalize_tool_failure("wechat_send", result)
    assert normalized.success is False
    assert normalized.error == "Contact not found"


def test_triage_wechat_fast_path_supports_natural_language(monkeypatch):
    from src.ceo.graph import triage_node
    import src.execution._wechat_tool as wechat_tool

    monkeypatch.setattr(
        wechat_tool,
        "send_wechat_message",
        lambda contact, message: {
            "success": True,
            "contact": contact,
            "message": message,
        },
    )

    result = asyncio.run(
        triage_node(_make_state("发送消息给微信联系人，小媛儿宝儿，发送消息：多喝水。"))
    )

    assert result["phase"] == "deliver"
    assert result["department"] == "devops"
    assert "小媛儿宝儿" in result["final_output"]
    assert "多喝水" in result["final_output"]


def test_wechat_action_can_control_ui_returns_bool():
    from src.wechat.action import WechatAction

    assert isinstance(WechatAction().can_control_ui(), bool)


def test_wechat_action_get_window_state_parses_applescript_output(monkeypatch):
    from src.wechat.action import WechatAction

    monkeypatch.setattr(
        WechatAction,
        "_run_get",
        lambda self, script, timeout=5: "2|true|true|false|false|false|105,149|368,332",
    )

    state = WechatAction().get_window_state()
    assert state == {
        "window_count": 2,
        "frontmost": True,
        "visible": True,
        "minimized": False,
        "main": False,
        "focused": False,
        "position": (105, 149),
        "size": (368, 332),
    }


def test_wechat_coordinator_reports_unfocused_window(monkeypatch):
    import src.wechat.action as action_mod
    import src.wechat.vision as vision_mod
    from src.wechat.coordinator import WechatCoordinator

    class FakeAction:
        def can_control_ui(self):
            return True

        def activate(self):
            return True

        def focus_window(self):
            return True

        def press_esc(self):
            return True

        def click(self, _x, _y):
            return True

        def get_window_rect(self):
            return (0, 0, 100, 100)

        def get_window_state(self):
            return {
                "window_count": 2,
                "frontmost": True,
                "visible": True,
                "minimized": False,
                "main": False,
                "focused": False,
                "position": (105, 149),
                "size": (368, 332),
            }

        def open_search(self):
            return True

    class FakeVision:
        def can_capture_screen(self):
            return True

        def check_wechat(self):
            return {"wechat_visible": True, "page": "other", "confidence": 0.9}

    monkeypatch.setattr(action_mod, "WechatAction", FakeAction)
    monkeypatch.setattr(vision_mod, "WechatVision", FakeVision)
    monkeypatch.setattr("src.wechat.coordinator.time.sleep", lambda *_args, **_kwargs: None)

    result = WechatCoordinator().send("小媛儿宝儿", "多喝水")
    assert result["success"] is False
    assert result["state"] == "CHECK_WECHAT"
    assert "current Space" in result["error"]


def test_wechat_coordinator_reports_missing_screen_recording(monkeypatch):
    import src.wechat.action as action_mod
    import src.wechat.vision as vision_mod
    from src.wechat.coordinator import WechatCoordinator

    class FakeAction:
        def can_control_ui(self):
            return True

    class FakeVision:
        def can_capture_screen(self):
            return False

    monkeypatch.setattr(action_mod, "WechatAction", FakeAction)
    monkeypatch.setattr(vision_mod, "WechatVision", FakeVision)

    result = WechatCoordinator().send("小媛儿宝儿", "多喝水")
    assert result["success"] is False
    assert result["state"] == "PRECHECK"
    assert "Screen Recording unavailable" in result["error"]


def test_wechat_action_type_text_can_paste_without_select_all(monkeypatch):
    from src.wechat.action import WechatAction

    captured = {}

    def fake_run(self, script, timeout=5):
        captured["script"] = script
        return True

    monkeypatch.setattr(WechatAction, "_run", fake_run)
    assert WechatAction().type_text("多喝水", select_all=False) is True
    assert 'keystroke "v" using command down' in captured["script"]
    assert 'keystroke "a" using command down' not in captured["script"]


def test_wechat_coordinator_requires_message_visible_before_success(monkeypatch):
    import src.wechat.action as action_mod
    import src.wechat.vision as vision_mod
    from src.wechat.coordinator import WechatCoordinator

    clicks = []
    typed = []

    class FakeAction:
        def get_window_rect(self):
            return (10, 20, 300, 400)

        def activate(self):
            return True

        def press_esc(self):
            return True

        def press_tab(self):
            return True

        def click(self, x, y):
            clicks.append((x, y))
            return True

        def type_text(self, text, select_all=True):
            typed.append((text, select_all))
            return True

    class FakeVision:
        def __init__(self):
            self.calls = 0

        def check_chat_open(self, _contact):
            return {
                "chat_open": True,
                "is_correct_contact": True,
                "input_visible": True,
                "input_center_x": 150,
                "input_center_y": 330,
            }

        def check_message_typed(self, message):
            self.calls += 1
            return {"message_in_input": self.calls == 1, "input_text": message if self.calls == 1 else ""}

    monkeypatch.setattr(action_mod, "WechatAction", FakeAction)
    monkeypatch.setattr(vision_mod, "WechatVision", FakeVision)
    monkeypatch.setattr("src.wechat.coordinator.time.sleep", lambda *_args, **_kwargs: None)

    ok, detail = WechatCoordinator()._type_and_verify_message("小媛儿宝儿", "多喝水")
    assert ok is True
    assert detail == "typed via vision(150,330)"
    assert typed == [("多喝水", False)]
    assert clicks == [(150, 330), (150, 330)]


def test_find_and_select_contact_requires_individual_for_normal_contact(monkeypatch):
    import src.wechat.action as action_mod
    import src.wechat.vision as vision_mod
    from src.wechat.coordinator import WechatCoordinator

    pressed_enter = []
    clicks = []

    class FakeAction:
        def type_text(self, text, select_all=True):
            return True

        def press_enter(self):
            pressed_enter.append(True)
            return True

        def click(self, x, y):
            clicks.append((x, y))
            return True

    class FakeVision:
        def find_contact(self, contact):
            return {
                "found": True,
                "contact_name": contact,
                "is_individual_contact": False,
                "center_x": 123,
                "center_y": 456,
            }

    monkeypatch.setattr(action_mod, "WechatAction", FakeAction)
    monkeypatch.setattr(vision_mod, "WechatVision", FakeVision)
    monkeypatch.setattr("src.wechat.coordinator.time.sleep", lambda *_args, **_kwargs: None)

    ok, detail = WechatCoordinator()._find_and_select_contact("小媛儿宝儿")
    assert ok is False
    assert "individual contact" in detail
    assert len(pressed_enter) == 3
    assert clicks == []


def test_find_and_select_contact_allows_filehelper_special_target(monkeypatch):
    import src.wechat.action as action_mod
    import src.wechat.vision as vision_mod
    from src.wechat.coordinator import WechatCoordinator

    clicks = []

    class FakeAction:
        def type_text(self, text, select_all=True):
            return True

        def press_enter(self):
            raise AssertionError("should not need enter fallback for special target")

        def click(self, x, y):
            clicks.append((x, y))
            return True

    class FakeVision:
        def find_contact(self, contact):
            return {
                "found": True,
                "contact_name": contact,
                "is_individual_contact": False,
                "center_x": 175,
                "center_y": 372,
            }

    monkeypatch.setattr(action_mod, "WechatAction", FakeAction)
    monkeypatch.setattr(vision_mod, "WechatVision", FakeVision)
    monkeypatch.setattr("src.wechat.coordinator.time.sleep", lambda *_args, **_kwargs: None)

    ok, detail = WechatCoordinator()._find_and_select_contact("文件传输助手")
    assert ok is True
    assert detail == "clicked(175,372)"
    assert clicks == [(175, 372)]
