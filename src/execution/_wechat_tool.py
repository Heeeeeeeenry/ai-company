"""WeChat Send Tool — delegates to vision-guided state machine.

Usage:
    python3 -m src.execution._wechat_tool send "contact" "message"
"""

import sys, json

def send_wechat_message(contact: str, message: str) -> dict:
    """Send WeChat message using vision-guided state machine."""
    from src.wechat import WechatCoordinator
    c = WechatCoordinator()
    return c.send(contact, message)

if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "send":
        print(json.dumps({"success": False, "error": "Usage: send <contact> <message>"}))
        sys.exit(1)
    print(json.dumps(send_wechat_message(sys.argv[2], sys.argv[3]), ensure_ascii=False))
