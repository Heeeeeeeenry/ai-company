"""WeChat Coordinator — vision-guided state machine for sending messages.

STATES:
  1. CHECK_WECHAT   → Verify WeChat is open/visible
  2. ENSURE_SEARCH  → Open search box, verify it appears
  3. FIND_CONTACT   → Type contact name, verify correct contact found
  4. OPEN_CHAT      → Click contact, verify chat window opens
  5. SEND_MESSAGE   → Click input, paste, verify message in input
  6. VERIFY_SENT    → Press Enter, verify message in chat history

GOAL-DRIVEN: "Chat with 张三 has message 你好 as last message"
Not step-driven: NOT "do Cmd+F then Enter then click"
"""

import time, logging
from typing import Optional

logger = logging.getLogger("ai_company.wechat")


class WechatCoordinator:
    """State machine for reliable WeChat message sending."""
    
    MAX_RETRIES_PER_STATE = 3
    
    def __init__(self):
        from src.wechat.vision import WechatVision
        from src.wechat.action import WechatAction
        self.vision = WechatVision()
        self.action = WechatAction()
    
    def send(self, contact: str, message: str, timeout: int = 60) -> dict:
        """Send a WeChat message. Returns {success, state, detail}."""
        # Safety: truncate long messages
        message = message[:500]
        start = time.time()
        
        # ─── Strategy: sidebar-first, search-fallback ───
        # Sidebar navigation (Cmd+1 → Down Arrow) is more reliable than
        # search (Cmd+F → paste → Enter) because:
        #   1. 文件传输助手 Enter triggers 搜一搜 instead of opening chat
        #   2. Search dropdown can close before vision-guided click lands
        #   3. Sidebar avoids garbage prefix from Cmd+A+Delete in search box
        result = self._try_sidebar_navigation(contact, message, timeout, start)
        if result is not None:
            return result
        
        # ─── Fallback: search flow ───
        
        # STATE 1: Check WeChat
        ok, detail = self._ensure_wechat_visible()
        if not ok:
            return {"success": False, "state": "CHECK_WECHAT", "error": detail}
        logger.info("VISION: WeChat visible")
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after CHECK_WECHAT"}
        
        # STATE 2: Open search
        ok, detail = self._ensure_search_open()
        if not ok:
            return {"success": False, "state": "ENSURE_SEARCH", "error": detail}
        logger.info("VISION: search box open")
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after SEARCH"}
        
        # STATE 3: Find contact
        ok, detail = self._find_and_select_contact(contact)
        if not ok:
            return {"success": False, "state": "FIND_CONTACT", "error": detail}
        logger.info("VISION: contact found and selected")
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after FIND_CONTACT"}
        
        # STATE 4: Verify chat open
        ok, detail = self._verify_chat_open(contact)
        if not ok:
            return {"success": False, "state": "OPEN_CHAT", "error": detail}
        logger.info("VISION: chat window open with correct contact")
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after OPEN_CHAT"}
        
        # STATE 5: Type message
        ok, detail = self._type_and_verify_message(message)
        if not ok:
            return {"success": False, "state": "SEND_MESSAGE", "error": detail}
        logger.info("VISION: message typed in input")
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after TYPE"}
        
        # STATE 6: Send and verify
        ok, detail = self._send_and_verify(message)
        if not ok:
            return {"success": False, "state": "VERIFY_SENT", "error": detail}
        
        elapsed = time.time() - start
        logger.info("VISION: message sent and verified in %.1fs", elapsed)
        return {
            "success": True,
            "contact": contact,
            "message": message,
            "output": f"Sent to {contact}",
            "elapsed": round(elapsed, 1),
        }
    
    # ─── State implementations ───
    
    def _ensure_wechat_visible(self) -> tuple:
        """STATE 1: Make sure WeChat is open and focused (CLI-first)."""
        import subprocess
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            # Fast check: is WeChat running?
            try:
                r = subprocess.run(["pgrep", "-l", "WeChat"], capture_output=True, text=True, timeout=3)
                if r.returncode != 0:
                    subprocess.run(["open", "-a", "WeChat"], timeout=5)
                    time.sleep(3)
                    continue
            except Exception:
                pass
            
            # Activate and check for window
            self.action.activate()
            time.sleep(2)
            rect = self.action.get_window_rect()
            if rect:
                return True, f"window at {rect}"
            
            time.sleep(1)
        
        # Fallback: try vision check
        try:
            result = self.vision.check_wechat()
            if result and result.get("wechat_visible"):
                return True, "vision"
        except Exception:
            pass
        
        return False, "WeChat not visible after retries"
    
    def _ensure_search_open(self) -> tuple:
        """STATE 2: Open search box, clear old text, verify with vision."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            self.action.open_search()
            time.sleep(1)
            # Clear any residual text from previous search (Agent A finding #4)
            self.action.clear_search_text()
            time.sleep(0.3)
            # Verify search box is open via vision
            result = self.vision.check_search_box()
            if result and result.get("search_open"):
                return True, f"search box visible (attempt {attempt+1})"
            time.sleep(1)
        return False, "Search box not opening after retries"
    
    def _find_and_select_contact(self, contact: str) -> tuple:
        """STATE 3: Type contact name, verify in results, open chat.

        For "文件传输助手": Enter opens 搜一搜 web search → use vision-guided
        click instead.  For normal contacts: press Enter after vision confirms.
        """
        is_file_transfer = (contact == "文件传输助手")

        for attempt in range(self.MAX_RETRIES_PER_STATE):
            # Type contact name (pbcopy + Cmd+V — supports Chinese)
            self.action.type_text(contact)
            time.sleep(1.5)

            # Verify contact appears in search results via Qwen-VL
            vision_result = self.vision.find_contact(contact)
            if not vision_result or not vision_result.get("found"):
                logger.warning("Vision: contact '%s' not confirmed in search (attempt %d)", contact, attempt+1)
                self.action.clear_search_text()
                time.sleep(0.5)
                continue

            logger.info("Vision: contact '%s' found in search results", contact)

            if is_file_transfer:
                # SPECIAL: 文件传输助手 — Enter triggers 搜一搜 instead of opening chat.
                # Use vision-guided click on the contact in the sidebar/search results.
                pos = self.vision.find_contact_position(contact)
                if pos and pos.get("screen_x"):
                    logger.info("Clicking 文件传输助手 at screen (%d, %d)", pos["screen_x"], pos["screen_y"])
                    self.action.click(pos["screen_x"], pos["screen_y"])
                    time.sleep(2)
                    return True, f"clicked at ({pos['screen_x']}, {pos['screen_y']})"
                # Fallback: try Enter anyway (might work on some WeChat versions)
                logger.warning("Vision click position unavailable, falling back to Enter")
                self.action.press_enter()
                time.sleep(2)
                return True, "entered (fallback)"

            # Normal contact: press Enter to open chat
            self.action.press_enter()
            time.sleep(2)
            return True, "entered"

        return False, f"Contact '{contact}' not found in search results"
    
    def _verify_chat_open(self, contact: str) -> tuple:
        """STATE 4: Verify chat window is open with correct contact using Qwen-VL."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            result = self.vision.check_chat_open(contact)
            if result and result.get("chat_open") and result.get("is_correct_contact"):
                return True, f"chat open with '{result.get('target_name', contact)}' (attempt {attempt+1})"
            logger.warning("Vision: chat not confirmed open for '%s' (attempt %d)", contact, attempt+1)
            time.sleep(1)
        return False, f"Chat not open with '{contact}' after retries"

    def _type_and_verify_message(self, message: str) -> tuple:
        """STATE 5: Click input field, select all (replace garbage), paste message, verify."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            # Click the input field first to ensure focus
            self.action.click_input_field()
            time.sleep(0.3)

            # Cmd+A to select all existing text (clears garbage from search operations)
            self.action.clear_search_text()  # Reuses Cmd+A+Delete pattern
            time.sleep(0.1)

            # Paste message via clipboard (pbcopy + Cmd/V — Chinese-safe)
            self.action.type_text(message)
            time.sleep(0.8)

            # Verify message is in input box via vision
            result = self.vision.check_message_typed(message)
            if result and result.get("message_in_input"):
                return True, f"message in input box (attempt {attempt+1})"
            logger.warning("Vision: message not confirmed in input box (attempt %d)", attempt+1)
            time.sleep(1)
        return False, "Message not confirmed in input box"
    
    def _send_and_verify(self, message: str) -> tuple:
        """STATE 6: Press Enter and verify message in chat."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            self.action.press_enter()
            time.sleep(1.5)
            
            result = self.vision.check_message_sent(message)
            if result and result.get("sent") and result.get("is_expected_message"):
                return True, "verified"
        return False, "Message not verified as sent"

    # ─── Sidebar-first navigation (unified) ───
    
    SIDEBAR_MAX_POSITION = 5  # Maximum Down Arrow attempts
    
    def _try_sidebar_navigation(
        self, contact: str, message: str, timeout: int, start: float
    ) -> Optional[dict]:
        """Try opening contact via sidebar chat list (Cmd+1 → Down×N → Enter).
        
        Returns result dict on success, None on failure (caller falls back to search).
        Tries positions 0..SIDEBAR_MAX_POSITION, verifying each with Qwen-VL.
        """
        ok, detail = self._ensure_wechat_visible()
        if not ok:
            return {"success": False, "state": "CHECK_WECHAT", "error": detail}
        
        for down_count in range(self.SIDEBAR_MAX_POSITION + 1):
            if time.time() - start > timeout:
                return {"success": False, "state": "TIMEOUT", "error": "Timeout in sidebar nav"}
            
            # Close any panels
            self.action.press_esc()
            time.sleep(0.3)
            self.action.press_esc()
            time.sleep(0.2)
            
            # Cmd+1 → Chat list tab
            self.action._run('''tell application "System Events"
                tell process "WeChat"
                    keystroke "1" using command down
                    delay 0.5
                end tell
            end tell
            return "ok"''')
            time.sleep(0.3)
            
            # Down Arrow × N (skip for N=0: first chat auto-selected)
            for _ in range(down_count):
                self.action._run('''tell application "System Events"
                    tell process "WeChat"
                        key code 125
                        delay 0.2
                    end tell
                end tell
                return "ok"''')
                time.sleep(0.15)
            
            # Enter → open chat
            self.action.press_enter()
            time.sleep(1.5)
            
            if time.time() - start > timeout:
                return {"success": False, "state": "TIMEOUT", "error": "Timeout after sidebar Enter"}
            
            # Verify with Qwen-VL
            result = self.vision.check_chat_open(contact)
            if result and result.get("chat_open") and result.get("is_correct_contact"):
                logger.info("Sidebar navigation: found '%s' at Down×%d", contact, down_count)
                break
            
            # Check if we just opened a different chat that starts with the contact name
            target = result.get("target_name", "") if result else ""
            if contact in str(target):
                logger.info("Sidebar: partial match '%s' at Down×%d, accepting", target, down_count)
                break
        else:
            # All positions exhausted
            return None  # Signal caller to fall back to search
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after sidebar find"}
        
        # Type and send
        ok, detail = self._type_and_verify_message(message)
        if not ok:
            return {"success": False, "state": "SEND_MESSAGE", "error": detail}
        
        ok, detail = self._send_and_verify(message)
        if not ok:
            return {"success": False, "state": "VERIFY_SENT", "error": detail}
        
        elapsed = time.time() - start
        return {
            "success": True,
            "contact": contact,
            "message": message,
            "output": f"Sent to {contact} via sidebar",
            "elapsed": round(elapsed, 1),
        }


def send_wechat_message(contact: str, message: str) -> dict:
    """Convenience function — same signature as old _wechat_tool."""
    coordinator = WechatCoordinator()
    return coordinator.send(contact, message)
