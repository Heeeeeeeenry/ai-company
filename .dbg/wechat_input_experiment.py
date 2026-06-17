import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.config import config  # noqa: F401,E402
from src.wechat.coordinator import WechatCoordinator  # noqa: E402
from src.wechat.action import WechatAction  # noqa: E402
from src.wechat.vision import WechatVision  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-input-experiment.json")
ROOT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg")


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
        }
    }
    rect = action.get_window_rect()
    payload["rect"] = rect
    payload["attempts"] = []
    if not rect:
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    x, y, w, h = rect
    for offset in (25, 45, 65, 90, 120):
        iy = y + h - offset
        ix = x + w // 2
        action.press_esc()
        time.sleep(0.3)
        action.click(ix, iy)
        time.sleep(0.3)
        action.type_text("多喝水", select_all=False)
        time.sleep(0.8)
        result = vision.check_message_typed("多喝水")
        shot = ROOT / f"wechat-input-offset-{offset}.png"
        subprocess.run(["screencapture", "-x", str(shot)], check=False)
        payload["attempts"].append({
            "offset": offset,
            "click": [ix, iy],
            "vision": result,
        })

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
