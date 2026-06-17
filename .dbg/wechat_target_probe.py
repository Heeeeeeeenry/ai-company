import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.config import config  # noqa: F401,E402
from src.wechat.coordinator import WechatCoordinator  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-target-probe.json")


def probe(target: str) -> dict:
    c = WechatCoordinator()
    return {
        "check_wechat": c._ensure_wechat_visible(),
        "search_open": c._ensure_search_open(),
        "find_contact": c._find_and_select_contact(target),
        "chat_open": c.vision.check_chat_open(target),
    }


def main() -> None:
    payload = {
        "filehelper": probe("文件传输助手"),
        "contact": probe("小媛儿宝儿"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run(["screencapture", "-x", "/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-target-probe.png"], check=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
