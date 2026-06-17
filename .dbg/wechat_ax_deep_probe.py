import json
import os
import subprocess
import tempfile
from pathlib import Path


OUT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg/wechat-ax-deep.json")


SCRIPT = r'''
tell application "System Events"
    tell process "WeChat"
        if (count of windows) is 0 then return "NO_WINDOWS"
        set linesOut to {}
        set idx to 0
        repeat with e in (entire contents of window 1)
            set idx to idx + 1
            if idx > 250 then exit repeat
            try
                set roleValue to value of attribute "AXRole" of e
            on error
                set roleValue to ""
            end try
            try
                set subroleValue to value of attribute "AXSubrole" of e
            on error
                set subroleValue to ""
            end try
            try
                set titleValue to title of e
            on error
                set titleValue to ""
            end try
            try
                set descValue to description of e
            on error
                set descValue to ""
            end try
            try
                set valValue to value of e as string
            on error
                set valValue to ""
            end try
            set end of linesOut to ((idx as string) & tab & roleValue & tab & subroleValue & tab & titleValue & tab & descValue & tab & valValue)
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
        result = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=30)
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
