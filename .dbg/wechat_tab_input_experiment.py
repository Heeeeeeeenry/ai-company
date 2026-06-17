import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.config import config  # noqa: F401,E402
from src.wechat.coordinator import WechatCoordinator  # noqa: E402
from src.wechat.action import WechatAction  # noqa: E402
from src.wechat.vision import WechatVision  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-tab-input-experiment.json")


def press_tab(action: WechatAction, count: int) -> None:
    script = '''tell application "System Events"
    tell process "WeChat"
        key code 48
        delay 0.2
    end tell
end tell
return "ok"'''
    for _ in range(count):
        action._run(script)


def main() -> None:
    coordinator = WechatCoordinator()
    action = WechatAction()
    vision = WechatVision()
    payload = {
        "setup": {
            "check_wechat": coordinator._ensure_wechat_visible(),
            "search_open": coordinator._ensure_search_open(),
            "find_contact": coordinator._find_and_select_contact("小媛儿宝儿"),
            "open_chat": coordinator._verify_chat_open("小媛儿宝儿"),
        },
        "attempts": [],
    }
    for tab_count in range(6):
        action.press_esc()
        time.sleep(0.2)
        press_tab(action, tab_count)
        action.type_text("多喝水", select_all=False)
        time.sleep(0.8)
        payload["attempts"].append({
            "tab_count": tab_count,
            "vision": vision.check_message_typed("多喝水"),
        })
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
