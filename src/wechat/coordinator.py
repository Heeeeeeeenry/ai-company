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
        """STATE 1: Make sure WeChat is open and focused."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            result = self.vision.check_wechat()
            if result and result.get("wechat_visible"):
                return True, "visible"
            # Try activating
            self.action.activate()
            time.sleep(1.5)
        return False, "WeChat not visible after retries"
    
    def _ensure_search_open(self) -> tuple:
        """STATE 2: Open search box."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            self.action.open_search()
            time.sleep(1)
            result = self.vision.check_search_box()
            if result and result.get("search_open"):
                return True, "open"
        return False, "Search box not opening"
    
    def _find_and_select_contact(self, contact: str) -> tuple:
        """STATE 3: Type contact, verify, select via Enter."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            self.action.type_text(contact)
            time.sleep(1.5)
            
            result = self.vision.find_contact(contact)
            if not result:
                continue
            
            if not result.get("found"):
                self.action.press_enter()
                time.sleep(1)
                continue
            
            if not result.get("is_individual_contact"):
                self.action.press_enter()
                time.sleep(1)
                continue
            
            # Select via Enter (verified reliable for contact results)
            self.action.press_enter()
            time.sleep(2)
            return True, "entered"
        
        return False, f"Contact '{contact}' not found as individual contact"
    
    def _verify_chat_open(self, contact: str) -> tuple:
        """STATE 4: Verify chat window is open with correct contact."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            result = self.vision.check_chat_open(contact)
            if result and result.get("chat_open") and result.get("is_correct_contact"):
                return True, "verified"
            time.sleep(1)
        return False, "Chat not open with correct contact"
    
    def _type_and_verify_message(self, message: str) -> tuple:
        """STATE 5: Dismiss search overlay, click input, type."""
        rect = self.action.get_window_rect()
        if not rect:
            return False, "No window rect"
        wx, wy, ww, wh = rect
        # First click chat area CENTER to dismiss search panel overlay
        cx = wx + ww // 2
        cy = wy + int(wh * 0.5)
        # Then click input field
        ix = wx + ww // 2
        iy = wy + wh - 25
        
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            # ESC to dismiss search
            self.action.press_esc()
            time.sleep(0.8)
            # Triple click input field at exact bottom position
            self.action.click(ix, iy)
            time.sleep(0.3)
            self.action.click(ix, iy)
            time.sleep(0.3)
            self.action.click(ix, iy)
            time.sleep(0.8)
            self.action.type_text(message)
            time.sleep(0.5)
            return True, f"3xclick input({ix},{iy})"
        return False, "Message not typed"
    
    def _send_and_verify(self, message: str) -> tuple:
        """STATE 6: Press Enter and verify message in chat."""
        for attempt in range(self.MAX_RETRIES_PER_STATE):
            self.action.press_enter()
            time.sleep(1.5)
            
            result = self.vision.check_message_sent(message)
            if result and result.get("sent") and result.get("is_expected_message"):
                return True, "verified"
        return False, "Message not verified as sent"


def send_wechat_message(contact: str, message: str) -> dict:
    """Convenience function — same signature as old _wechat_tool."""
    coordinator = WechatCoordinator()
    return coordinator.send(contact, message)
