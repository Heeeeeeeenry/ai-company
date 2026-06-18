"""WeChat input via macOS Accessibility API.

Falls back to clipboard paste when Accessibility is unavailable.
The Accessibility approach directly sets the text field value,
bypassing WeChat's paste-blocking behavior.

Usage:
    from src.wechat.input import type_text_via_accessibility
    type_text_via_accessibility("你好世界")
"""

import subprocess
import time
import logging

logger = logging.getLogger("ai_company.wechat.input")


def _run_applescript(script: str, timeout: int = 5) -> str:
    """Run AppleScript and return stdout."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def type_text_via_accessibility(text: str) -> bool:
    """Type text into WeChat input using Accessibility API.
    
    Strategy: find the focused text area and set its value directly.
    Falls back to clipboard paste if accessibility fails.
    """
    text = text[:500]
    
    # ═══ Method 1: Set value of focused text field via AX ═══
    # This bypasses the paste-blocking by setting the text property directly.
    
    script = '''
    use framework "AppKit"
    use scripting additions
    
    -- Get the focused element
    set sysElement to current application's AXUIElementCreateSystemWide()
    set focusedApp to missing value
    tell sysElement to set {focusedApp} to (getAttribute:"AXFocusedApplication") as list
    if focusedApp is missing value then return "NO_FOCUS"
    
    set focusedElement to missing value
    tell focusedApp to set {focusedElement} to (getAttribute:"AXFocusedUIElement") as list
    if focusedElement is missing value then return "NO_ELEMENT"
    
    -- Check if it's a text field/text area
    set elementRole to missing value
    tell focusedElement to set {elementRole} to (getAttribute:"AXRole") as list
    if elementRole is not "AXTextArea" and elementRole is not "AXTextField" then
        return "WRONG_ROLE:" & (elementRole as text)
    end if
    
    -- Set the value
    tell focusedElement to setAttribute:"AXValue" toValue:"''' + text.replace('"', '\\"') + '''"
    return "OK"
    '''
    
    result = _run_applescript(script, timeout=10)
    
    if result == "OK":
        logger.info("Accessibility: text set via AXValue")
        return True
    
    if result.startswith("NO_") or result.startswith("WRONG_"):
        logger.warning("Accessibility: %s — falling back to clipboard paste", result)
    else:
        logger.warning("Accessibility returned: %s", result[:100])
    
    # ═══ Method 2: Type text via AppleScript keystroke (ASCII only) ═══
    # This works for simple text but NOT for Chinese characters
    if all(ord(c) < 128 for c in text):
        escaped = text.replace('\\', '\\\\').replace('"', '\\"')
        script2 = f'''
        tell application "System Events"
            tell process "WeChat"
                keystroke "{escaped}"
            end tell
        end tell
        '''
        result2 = _run_applescript(script2, timeout=5)
        if result2 == "":
            logger.info("Keystroke: text typed via AppleScript")
            return True
    
    # ═══ Method 3: Clipboard paste (pbcopy + Cmd+V) ═══
    logger.info("Falling back to clipboard paste")
    return _paste_via_clipboard(text)


def _paste_via_clipboard(text: str) -> bool:
    """Set clipboard via pbcopy, then Cmd+V paste."""
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=3)
    except Exception:
        return False
    
    time.sleep(0.2)
    script = '''
    tell application "System Events"
        tell process "WeChat"
            keystroke "v" using command down
            delay 0.5
        end tell
    end tell
    '''
    result = _run_applescript(script, timeout=5)
    return result != ""
