"""WeChat Send Tool — delegates to vision-guided state machine.

Usage:
    python3 -m src.execution._wechat_tool send "contact" "message"
"""

import sys, json
import os
import urllib.request


def _debug_report(hypothesis_id: str, location: str, msg: str, data: dict | None = None) -> None:
    # #region debug-point C:wechat-tool-report
    env_path = ".dbg/wechat-send-fail.env"
    server_url = os.environ.get("DEBUG_SERVER_URL", "")
    session_id = os.environ.get("DEBUG_SESSION_ID", "")
    run_id = os.environ.get("DEBUG_RUN_ID", "pre-fix")
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("DEBUG_SERVER_URL="):
                    server_url = line.split("=", 1)[1].strip()
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip()
    except OSError:
        pass
    if not server_url or not session_id:
        return
    payload = {
        "sessionId": session_id,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "msg": msg,
        "data": data or {},
    }
    try:
        request = urllib.request.Request(
            server_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=2).read()
    except Exception:
        pass
    # #endregion

def send_wechat_message(contact: str, message: str) -> dict:
    """Send WeChat message using vision-guided state machine."""
    from src.wechat import WechatCoordinator
    _debug_report(
        "C",
        "src/execution/_wechat_tool.py:send_wechat_message:start",
        "[DEBUG] Invoking WeChat coordinator",
        {"contact": contact, "message": message[:100]},
    )
    c = WechatCoordinator()
    result = c.send(contact, message)
    _debug_report(
        "C",
        "src/execution/_wechat_tool.py:send_wechat_message:done",
        "[DEBUG] WeChat coordinator returned",
        {"result": result},
    )
    return result

if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "send":
        print(json.dumps({"success": False, "error": "Usage: send <contact> <message>"}))
        sys.exit(1)
    print(json.dumps(send_wechat_message(sys.argv[2], sys.argv[3]), ensure_ascii=False))
