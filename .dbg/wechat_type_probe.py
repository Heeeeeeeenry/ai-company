import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.config import config  # noqa: F401,E402
from src.wechat.coordinator import WechatCoordinator  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-type-probe.json")
SHOT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-type-probe.png")


def main() -> None:
    c = WechatCoordinator()
    payload = {
        "check_wechat": c._ensure_wechat_visible(),
        "search_open": c._ensure_search_open(),
        "find_contact": c._find_and_select_contact("小媛儿宝儿"),
        "open_chat": c._verify_chat_open("小媛儿宝儿"),
        "type_message": c._type_and_verify_message("多喝水"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run(["screencapture", "-x", str(SHOT)], check=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
