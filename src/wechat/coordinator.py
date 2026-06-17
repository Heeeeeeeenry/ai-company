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
        
        # ─── SPECIAL: 文件传输助手 ───
        # Cannot be opened via search (Enter triggers 搜一搜 web search).
        # Must use sidebar chat list: Cmd+1 → Down Arrow → Enter
        if contact == "文件传输助手":
            return self._send_to_file_transfer(message, timeout, start)
        
        # ─── Normal contacts ───
        
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
            # FINAL SAFETY CHECK: message might have been sent but vision missed it.
            # If the message is already in the chat, declare success to prevent
            # caller retries that would send duplicates.
            logger.warning("Send+verify failed, checking if message was already sent...")
            time.sleep(2)
            sent_result = self.vision.check_message_sent(message)
            if sent_result and sent_result.get("sent") and sent_result.get("is_expected_message"):
                logger.info("VISION: message was already sent (belated verification)")
                ok, detail = True, "verified (delayed)"
        
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

            # Verify contact appears in search results via Qwen-VL (exact match only)
            vision_result = self.vision.find_contact(contact)
            if not vision_result or not vision_result.get("found") or not vision_result.get("is_exact_match"):
                logger.warning("Vision: contact '%s' not exactly found (attempt %d): %s", 
                             contact, attempt+1, vision_result.get("contact_name","?") if vision_result else "None")
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
        """STATE 6: Press Enter ONCE, then retry vision verification only.

        CRITICAL: Never press Enter on retry — if the first Enter worked
        but vision didn't catch it, retrying Enter would send the message
        again (or worse: the caller retries the whole flow, sending duplicates).
        """
        self.action.press_enter()
        time.sleep(1.5)
        
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            result = self.vision.check_message_sent(message)
            if result and result.get("sent") and result.get("is_expected_message"):
                return True, "verified"
            logger.warning("Vision: message not verified in chat (attempt %d)", attempt+1)
            if attempt < self.MAX_RETRIES_PER_STATE - 1:
                time.sleep(2)  # wait longer for chat to update before re-check
        
        return False, "Message not verified as sent"

    # ─── 文件传输助手 special handler ───
    
    def _send_to_file_transfer(self, message: str, timeout: int, start: float) -> dict:
        """Send to 文件传输助手 via sidebar chat list navigation.
        
        WeChat macOS design: 文件传输助手 is a 功能 (function) entry, not a contact.
        Search + Enter triggers 搜一搜 web search instead of opening the chat.
        The only reliable way: Cmd+1 → sidebar → Down Arrow ×1 → Enter.
        (文件传输助手 is consistently the 2nd entry in the chat list.)
        """
        import subprocess as _sp
        
        # Ensure WeChat is running
        ok, detail = self._ensure_wechat_visible()
        if not ok:
            return {"success": False, "state": "CHECK_WECHAT", "error": detail}
        
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            # Close any search panels
            self.action.press_esc()
            time.sleep(0.3)
            self.action.press_esc()
            time.sleep(0.3)
            
            # Cmd+1 → Chat list tab
            self.action._run('''tell application "System Events"
                tell process "WeChat"
                    keystroke "1" using command down
                    delay 0.8
                end tell
            end tell
            return "ok"''')
            time.sleep(0.5)
            
            # Down Arrow → select 文件传输助手 (2nd entry)
            self.action._run('''tell application "System Events"
                tell process "WeChat"
                    key code 125
                    delay 0.3
                end tell
            end tell
            return "ok"''')
            time.sleep(0.3)
            
            # Enter → open chat
            self.action.press_enter()
            time.sleep(1.5)
            
            if time.time() - start > timeout:
                return {"success": False, "state": "TIMEOUT", "error": "Timeout"}
            
            # Verify chat is open with 文件传输助手
            result = self.vision.check_chat_open("文件传输助手")
            if result and result.get("chat_open") and result.get("is_correct_contact"):
                break
            
            logger.warning("File transfer chat not open (attempt %d), retrying...", attempt + 1)
            time.sleep(0.5)
        else:
            return {"success": False, "state": "OPEN_CHAT", 
                    "error": "Cannot open 文件传输助手 chat via sidebar"}
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after open"}
        
        # Type message (pbcopy + Cmd+V)
        ok, detail = self._type_and_verify_message(message)
        if not ok:
            return {"success": False, "state": "SEND_MESSAGE", "error": detail}
        
        if time.time() - start > timeout:
            return {"success": False, "state": "TIMEOUT", "error": "Timeout after type"}
        
        # Send and verify
        ok, detail = self._send_and_verify(message)
        if not ok:
            return {"success": False, "state": "VERIFY_SENT", "error": detail}
        
        elapsed = time.time() - start
        return {
            "success": True,
            "contact": "文件传输助手",
            "message": message,
            "output": "Sent to 文件传输助手",
            "elapsed": round(elapsed, 1),
        }


def send_wechat_message(contact: str, message: str) -> dict:
    """Convenience function — same signature as old _wechat_tool."""
    coordinator = WechatCoordinator()
    return coordinator.send(contact, message)
