"""WeChat Vision Agent — screenshot + Qwen-VL structured analysis."""

import json, os, base64, subprocess, tempfile
from io import BytesIO
from PIL import Image
from typing import Optional


PROMPT_CHECK_WECHAT = """Analyze this screenshot. Is WeChat visible?
Return ONLY JSON:
{"wechat_visible": true/false, "page": "chat_list|chat_window|search|other|not_visible", "confidence": 0.0-1.0}"""

PROMPT_SEARCH_BOX = """Analyze this WeChat screenshot. Is the search box open and visible?
Return ONLY JSON:
{"search_open": true/false, "search_text": "text in search box", "confidence": 0.0-1.0}"""

PROMPT_FIND_CONTACT = """Analyze this WeChat search result screenshot.
Looking for contact: __CONTACT__
Return ONLY JSON:
{"found": true/false, "contact_name": "exact name found", "is_individual_contact": true/false, "position": "first|second|not_found", "center_x": pixel_x, "center_y": pixel_y, "confidence": 0.0-1.0}"""

PROMPT_CHAT_OPEN = """Analyze this WeChat screenshot. Expected chat with: __CONTACT__
Return ONLY JSON:
{"chat_open": true/false, "target_name": "name at top", "is_correct_contact": true/false, "input_visible": true/false, "input_center_x": pixel_x, "input_center_y": pixel_y, "confidence": 0.0-1.0}"""

PROMPT_MESSAGE_TYPED = """Analyze this WeChat screenshot. Check if message input contains: __MESSAGE__
Return ONLY JSON:
{"message_in_input": true/false, "input_text": "text visible in input", "confidence": 0.0-1.0}"""

PROMPT_MESSAGE_SENT = """Analyze this WeChat screenshot. Check if the LAST message in chat is: __MESSAGE__
Return ONLY JSON:
{"sent": true/false, "last_message": "exact text of last message", "is_expected_message": true/false, "confidence": 0.0-1.0}"""


class WechatVision:
    """Screenshot + Qwen-VL analysis for each WeChat state."""
    
    def __init__(self):
        self._client = None
        self._model = os.environ.get("QWEN_VISION_MODEL", "qwen-vl-max")
    
    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=os.environ.get("QWEN_API_KEY", ""),
                base_url=os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            )
        return self._client
    
    def _capture(self) -> Image.Image:
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=5)
            img = Image.open(path).convert("RGB")
            img.load()
            return img
        finally:
            try: os.unlink(path)
            except OSError: pass
    
    def _ask(self, image: Image.Image, prompt: str, max_tokens: int = 200) -> Optional[dict]:
        w, h = image.size
        if w > 1280:
            image = image.resize((1280, int(h * 1280 / w)), Image.LANCZOS)
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode()
        try:
            client = self._get_client()
            resp = client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]}],
                max_tokens=max_tokens,
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception:
            return None
    
    # ─── State checks ───
    
    def check_wechat(self) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_CHECK_WECHAT)
    
    def check_search_box(self) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_SEARCH_BOX)
    
    def find_contact(self, contact: str) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_FIND_CONTACT.replace("__CONTACT__", contact))
    
    def check_chat_open(self, contact: str) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_CHAT_OPEN.replace("__CONTACT__", contact))
    
    def check_message_typed(self, message: str) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_MESSAGE_TYPED.replace("__MESSAGE__", message))
    
    def check_message_sent(self, message: str) -> Optional[dict]:
        img = self._capture()
        return self._ask(img, PROMPT_MESSAGE_SENT.replace("__MESSAGE__", message))
