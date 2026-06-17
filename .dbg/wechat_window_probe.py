import json
import subprocess
import time
from pathlib import Path


ROOT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company")
SCREENSHOT = ROOT / ".dbg" / "wechat-window-probe.png"


def run_osa(script: str) -> dict:
    result = subprocess.run(
        ["osascript", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return {
        "code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


WINDOW_ENUM_SCRIPT = r'''
tell application "WeChat" to activate
delay 1
tell application "System Events"
    tell process "WeChat"
        set outputLines to {}
        set idx to 0
        repeat with w in windows
            set idx to idx + 1
            try
                set titleValue to name of w
            on error
                set titleValue to ""
            end try
            try
                set roleValue to value of attribute "AXRole" of w
            on error
                set roleValue to ""
            end try
            try
                set subroleValue to value of attribute "AXSubrole" of w
            on error
                set subroleValue to ""
            end try
            try
                set mainValue to value of attribute "AXMain" of w
            on error
                set mainValue to false
            end try
            try
                set focusedValue to value of attribute "AXFocused" of w
            on error
                set focusedValue to false
            end try
            try
                set minimizedValue to value of attribute "AXMinimized" of w
            on error
                set minimizedValue to false
            end try
            try
                set positionValue to position of w
            on error
                set positionValue to {0, 0}
            end try
            try
                set sizeValue to size of w
            on error
                set sizeValue to {0, 0}
            end try
            set end of outputLines to ((idx as string) & tab & titleValue & tab & roleValue & tab & subroleValue & tab & (mainValue as string) & tab & (focusedValue as string) & tab & (minimizedValue as string) & tab & ((item 1 of positionValue) as string) & "," & ((item 2 of positionValue) as string) & tab & ((item 1 of sizeValue) as string) & "," & ((item 2 of sizeValue) as string))
        end repeat
        return outputLines as string
    end tell
end tell
'''


FRONTMOST_SCRIPT = r'''
tell application "System Events"
    tell process "WeChat"
        return (frontmost as string) & tab & (visible as string) & tab & (count of windows as string)
    end tell
end tell
'''


def main() -> None:
    payload = {
        "frontmost": run_osa(FRONTMOST_SCRIPT),
        "windows": run_osa(WINDOW_ENUM_SCRIPT),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    time.sleep(1)
    subprocess.run(["screencapture", "-x", str(SCREENSHOT)], check=False)


if __name__ == "__main__":
    main()
