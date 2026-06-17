"""WeChat Action Agent — execute UI actions via AppleScript.

All coordinates are in SCREEN POINTS (not retina pixels).
Uses click at + keystroke, NOT fixed-position clicking.
"""

import subprocess, tempfile, os, time
from typing import Optional


# Single reusable script templates

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

PASTE_AND_SEND = '''tell application "System Events"
    tell process "WeChat"
        set the clipboard to "{message}"
        delay 0.2
        keystroke "v" using command down
        delay 0.3
        keystroke return
    end tell
end tell
return "ok"'''

GET_WINDOW = '''tell application "System Events"
    tell process "WeChat"
        set wp to position of window 1
        set ws to size of window 1
        return ((item 1 of wp) as string) & "," & ((item 2 of wp) as string) & "," & ((item 1 of ws) as string) & "," & ((item 2 of ws) as string)
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
        """Bring WeChat to foreground."""
        return self._run(ACTIVATE)
    
    def open_search(self) -> bool:
        """Cmd+F to open search."""
        return self._run(OPEN_SEARCH)
    
    def type_text(self, text: str) -> bool:
        """Type text via clipboard paste. Truncates long messages."""
        # Truncate to prevent clipboard overflow / accidental paste of large content
        text = text[:500]
        escaped = text.replace('"', '\\"')
        return self._run(TYPE_TEXT.format(text=escaped))
    
    def press_enter(self) -> bool:
        """Press Enter."""
        return self._run(PRESS_ENTER)
    
    def press_esc(self) -> bool:
        """Press Escape."""
        return self._run(PRESS_ESC)
    
    def click(self, x: int, y: int) -> bool:
        """Click at screen coordinates (points)."""
        return self._run(CLICK_AT.format(x=x, y=y))
    
    def paste_and_send(self, message: str) -> bool:
        """Paste message into current field and press Enter."""
        escaped = message.replace('"', '\\"')
        return self._run(PASTE_AND_SEND.format(message=escaped))
    
    def get_window_rect(self) -> Optional[tuple]:
        """Get WeChat window position and size. Returns (x, y, w, h) or None."""
        result = self._run_get(GET_WINDOW)
        if result:
            parts = result.split(",")
            if len(parts) == 4:
                return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        return None
    
    def click_input_field(self) -> bool:
        """Click the message input field (center, 30px from bottom)."""
        rect = self.get_window_rect()
        if rect is None:
            return False
        wx, wy, ww, wh = rect
        ix = wx + ww // 2
        iy = wy + wh - 30
        return self.click(ix, iy)
