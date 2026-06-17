import json
import os
import subprocess
import tempfile
from pathlib import Path


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-menu-probe.json")


SCRIPT = r'''
tell application "System Events"
    tell process "WeChat"
        set linesOut to {}
        repeat with m in menu bar items of menu bar 1
            try
                set menuTitle to title of m
            on error
                set menuTitle to ""
            end try
            set end of linesOut to menuTitle
            try
                repeat with mi in menu items of menu 1 of m
                    try
                        set itemTitle to title of mi
                    on error
                        set itemTitle to ""
                    end try
                    if itemTitle is not "" then
                        set end of linesOut to ("  - " & itemTitle)
                    end if
                end repeat
            end try
        end repeat
        return linesOut as string
    end tell
end tell
'''


def main() -> None:
    fd, path = tempfile.mkstemp(suffix=".scpt")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(SCRIPT)
        result = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=20)
        OUT.write_text(json.dumps({
            "code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"code": result.returncode}, ensure_ascii=False))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
