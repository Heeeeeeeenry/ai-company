"""WeChat Action Agent — execute UI actions via AppleScript.

All coordinates in SCREEN POINTS. No & in AppleScript (shell compatibility).
"""

import subprocess, tempfile, os, time, json
from typing import Optional


ACTIVATE = '''tell application "WeChat" to activate
delay 1
tell application "System Events"
    tell process "WeChat"
        set frontmost to true
        delay 0.5
    end tell
end tell
return "ok"'''

OPEN_SEARCH = '''tell application "System Events"
    tell process "WeChat"
        keystroke "f" using command down
        delay 0.5
    end tell
end tell
return "ok"'''

TYPE_TEXT = '''tell application "System Events"
    tell process "WeChat"
        set the clipboard to "{text}"
        delay 0.2
        keystroke "a" using command down
        delay 0.1
        keystroke "v" using command down
        delay 0.3
    end tell
end tell
return "ok"'''

PRESS_ENTER = '''tell application "System Events"
    tell process "WeChat"
        keystroke return
        delay 0.5
    end tell
end tell
return "ok"'''

PRESS_ESC = '''tell application "System Events"
    tell process "WeChat"
        key code 53
        delay 0.3
    end tell
end tell
return "ok"'''

CLICK_AT = '''tell application "System Events"
    tell process "WeChat"
        click at {{{x}, {y}}}
        delay 0.3
    end tell
end tell
return "ok"'''

# NO & — uses AppleScript list return
GET_WINDOW = '''tell application "System Events"
    tell process "WeChat"
        set wp to position of window 1
        set ws to size of window 1
        set wx to item 1 of wp
        set wy to item 2 of wp
        set ww to item 1 of ws
        set wh to item 2 of ws
        return {wx, wy, ww, wh}
    end tell
end tell'''


class WechatAction:
    """Execute WeChat UI actions via AppleScript."""
    
    def _run(self, script: str, timeout: int = 5) -> bool:
        fd, path = tempfile.mkstemp(suffix=".scpt")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write(script)
            r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0
        except Exception:
            return False
        finally:
            try: os.unlink(path)
            except OSError: pass
    
    def _run_get(self, script: str, timeout: int = 5) -> Optional[str]:
        fd, path = tempfile.mkstemp(suffix=".scpt")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write(script)
            r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
        finally:
            try: os.unlink(path)
            except OSError: pass
    
    def activate(self) -> bool:
        return self._run(ACTIVATE)
    
    def open_search(self) -> bool:
        return self._run(OPEN_SEARCH)
    
    def type_text(self, text: str) -> bool:
        text = text[:500]
        escaped = text.replace('"', '\\"')
        return self._run(TYPE_TEXT.format(text=escaped))
    
    def press_enter(self) -> bool:
        return self._run(PRESS_ENTER)
    
    def press_esc(self) -> bool:
        return self._run(PRESS_ESC)
    
    def click(self, x: int, y: int) -> bool:
        return self._run(CLICK_AT.format(x=x, y=y))
    
    def get_window_rect(self) -> Optional[tuple]:
        """Returns (x, y, w, h) or None. Parses AppleScript list format."""
        result = self._run_get(GET_WINDOW)
        if not result:
            return None
        # AppleScript list: "123, 456, 789, 101" — parse comma-separated
        parts = [p.strip() for p in result.split(",")]
        if len(parts) == 4:
            try:
                return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            except ValueError:
                return None
        return None
    
    def click_input_field(self) -> bool:
        """Click message input field (center, 25px from bottom)."""
        rect = self.get_window_rect()
        if rect is None:
            return False
        wx, wy, ww, wh = rect
        return self.click(wx + ww // 2, wy + wh - 25)
