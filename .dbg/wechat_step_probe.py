import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.wechat.coordinator import WechatCoordinator


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-step-probe.json")


def safe_call(fn, *args):
    try:
        return {"ok": True, "result": fn(*args)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    c = WechatCoordinator()
    payload = {
        "preflight": {
            "can_control_ui": c.action.can_control_ui(),
            "can_capture_screen": c.vision.can_capture_screen(),
            "window_state": c.action.get_window_state(),
        },
        "check_wechat": safe_call(c._ensure_wechat_visible),
        "search_open": safe_call(c._ensure_search_open),
        "find_contact": safe_call(c._find_and_select_contact, "小媛儿宝儿"),
        "open_chat": safe_call(c._verify_chat_open, "小媛儿宝儿"),
        "type_message": safe_call(c._type_and_verify_message, "多喝水"),
        "verify_sent": safe_call(c._send_and_verify, "多喝水"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
