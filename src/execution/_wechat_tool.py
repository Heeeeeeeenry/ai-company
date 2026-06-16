"""WeChat Send Tool — TWO-STEP AppleScript (verified working).

Step 1: Clear state, search, Enter (opens chat)
Step 2: Get window pos, click input, paste, Enter (sends)

CRITICAL: Must be two separate osascript calls. Single-script fails
because WeChat needs time between search and Enter for UI to render.

Usage:
    python3 -m src.execution._wechat_tool send "contact" "message"
"""

import subprocess, sys, json, tempfile, os, time

STEP1 = '''tell application "WeChat" to activate
delay 1.5
tell application "System Events"
    tell process "WeChat"
        set frontmost to true
        delay 0.5
        -- Clear any existing search
        key code 53
        delay 0.5
        -- Cmd+F: search
        keystroke "f" using command down
        delay 0.8
        -- Type contact
        set the clipboard to "{contact}"
        delay 0.3
        keystroke "a" using command down
        delay 0.2
        keystroke "v" using command down
        delay 1.5
        -- Enter: open first result
        keystroke return
        delay 1.0
    end tell
end tell
return "done"
'''

STEP2 = '''tell application "WeChat" to activate
delay 0.5
tell application "System Events"
    tell process "WeChat"
        set frontmost to true
        delay 0.5
        -- Get window position NOW
        set wp to position of window 1
        set ws to size of window 1
        set ix to (item 1 of wp) + (item 1 of ws) / 2
        set iy to (item 2 of wp) + (item 2 of ws) - 30
        -- Click input field
        click at {{ix, iy}}
        delay 0.5
        -- Paste message
        set the clipboard to "{message}"
        delay 0.3
        keystroke "v" using command down
        delay 0.5
        -- Send
        keystroke return
    end tell
end tell
return "done"
'''

def _run_script(script: str, timeout: int = 15) -> tuple:
    fd, path = tempfile.mkstemp(suffix=".scpt")
    os.close(fd)
    try:
        with open(path, "w") as f:
            f.write(script)
        r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    finally:
        try: os.unlink(path)
        except OSError: pass

def send_wechat_message(contact: str, message: str, timeout: int = 25) -> dict:
    """Two-step WeChat send."""
    ce = contact.replace('"', '\\"')
    me = message.replace('"', '\\"')
    
    # Step 1: Search and open chat
    ok, out, err = _run_script(STEP1.format(contact=ce), timeout=12)
    if not ok:
        return {"success": False, "error": f"Search failed: {err or out}"}
    
    # Wait for chat to fully load
    time.sleep(2.5)
    
    # Step 2: Click input + paste + send
    ok, out, err = _run_script(STEP2.format(message=me), timeout=10)
    return {
        "success": ok,
        "contact": contact, "message": message,
        "output": f"Sent to {contact}" if ok else (err or "failed"),
        "error": None if ok else (err or "osascript failed"),
    }

if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "send":
        print(json.dumps({"success": False, "error": "Usage: send <contact> <message>"}))
        sys.exit(1)
    print(json.dumps(send_wechat_message(sys.argv[2], sys.argv[3]), ensure_ascii=False))
