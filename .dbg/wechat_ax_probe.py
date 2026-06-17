import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path("/Users/v_liheng02/.openclaw/workspace/ai-company/.dbg")
ALL_FILE = ROOT / "wechat-ax-elements.json"
TEXT_FILE = ROOT / "wechat-ax-text-fields.json"


def run_script(script: str, timeout: int = 20) -> dict:
    fd, path = tempfile.mkstemp(suffix=".scpt")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        result = subprocess.run(
            ["osascript", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


ALL_ELEMENTS_SCRIPT = r'''
tell application "System Events"
    tell process "WeChat"
        if (count of windows) is 0 then return "NO_WINDOWS"
        set linesOut to {}
        tell window 1
            set idx to 0
            repeat with e in (every UI element)
                set idx to idx + 1
                if idx > 80 then exit repeat
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
        end tell
        return linesOut as string
    end tell
end tell
'''


TEXT_FIELDS_SCRIPT = r'''
tell application "System Events"
    tell process "WeChat"
        if (count of windows) is 0 then return "NO_WINDOWS"
        set linesOut to {}
        tell window 1
            set idx to 0
            repeat with e in (every text field)
                set idx to idx + 1
                try
                    set posValue to position of e
                on error
                    set posValue to {0, 0}
                end try
                try
                    set sizeValue to size of e
                on error
                    set sizeValue to {0, 0}
                end try
                try
                    set valValue to value of e as string
                on error
                    set valValue to ""
                end try
                set end of linesOut to ((idx as string) & tab & ((item 1 of posValue) as string) & "," & ((item 2 of posValue) as string) & tab & ((item 1 of sizeValue) as string) & "," & ((item 2 of sizeValue) as string) & tab & valValue)
            end repeat
        end tell
        return linesOut as string
    end tell
end tell
'''


def main() -> None:
    all_result = run_script(ALL_ELEMENTS_SCRIPT)
    text_result = run_script(TEXT_FIELDS_SCRIPT)
    ALL_FILE.write_text(json.dumps(all_result, ensure_ascii=False, indent=2), encoding="utf-8")
    TEXT_FILE.write_text(json.dumps(text_result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"all": all_result["code"], "text": text_result["code"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
