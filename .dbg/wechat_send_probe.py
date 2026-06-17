import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/Users/v_liheng02/.openclaw/workspace/ai-company")

from src.config import config  # noqa: F401,E402
from src.wechat.coordinator import WechatCoordinator  # noqa: E402


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-send-probe.json")
SHOT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-send-probe.png")


def main() -> None:
    coordinator = WechatCoordinator()
    result = coordinator.send("小媛儿宝儿", "多喝水")
    payload = {"result": result}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run(["screencapture", "-x", str(SHOT)], check=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
