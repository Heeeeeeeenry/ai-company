"""WeChat Vision Agent — screenshot + Qwen-VL structured analysis."""

import json, os, base64, subprocess, tempfile, ctypes
from typing import Optional

try:
    from io import BytesIO
    from PIL import Image
except Exception:  # Pillow is optional at runtime.
    BytesIO = None
    Image = None


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

PROMPT_MESSAGE_TYPED = """Analyze this WeChat screenshot. Check whether the BOTTOM chat composer/input box (where a message would be sent to the current chat) contains: __MESSAGE__
Important:
- ONLY inspect the bottom message composer in the active chat window.
- DO NOT treat the left sidebar search box, global search overlay, or any search field as the message input box.
Return ONLY JSON:
{"message_in_input": true/false, "input_text": "text visible in the bottom chat composer", "confidence": 0.0-1.0}"""

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

    def can_capture_screen(self) -> bool:
        """Best-effort macOS Screen Recording preflight."""
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
        except Exception:
            # If the API is unavailable, avoid blocking the flow on a false negative.
            return True
    
    def _capture(self, return_geometry: bool = False):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=5)
            if Image is not None and BytesIO is not None:
                img = Image.open(path).convert("RGB")
                img.load()
                orig_w, orig_h = img.size
                # Crop center 75% to avoid dock/menubar/side apps (also avoids Qwen content filter)
                left = orig_w // 8
                top = orig_h // 15
                right = 7 * orig_w // 8
                bottom = 14 * orig_h // 15
                img = img.crop((left, top, right, bottom))
                crop_w, crop_h = img.size
                scale = 1.0
                if crop_w > 960:
                    scale = 960.0 / crop_w
                    img = img.resize((960, int(crop_h * scale)), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=50)
                if return_geometry:
                    return buf.getvalue(), {
                        "screen_w": orig_w, "screen_h": orig_h,
                        "crop_left": left, "crop_top": top,
                        "crop_w": crop_w, "crop_h": crop_h,
                        "scale": scale,
                    }
                return buf.getvalue()

            # Fallback: use macOS sips to resize/compress without Pillow.
            resized_path = f"{path}.jpg"
            subprocess.run(
                ["sips", "-Z", "1280", "-s", "format", "jpeg", path, "--out", resized_path],
                capture_output=True,
                timeout=10,
            )
            output_path = resized_path if os.path.exists(resized_path) else path
            with open(output_path, "rb") as handle:
                data = handle.read()
            if return_geometry:
                # Fallback: estimate geometry (no Pillow, best-effort)
                return data, {
                    "screen_w": 0, "screen_h": 0,
                    "crop_left": 0, "crop_top": 0,
                    "crop_w": 0, "crop_h": 0,
                    "scale": 1.0,
                }
            return data
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
            try:
                os.unlink(f"{path}.jpg")
            except OSError:
                pass
    
    def _ask(self, image_bytes: bytes, prompt: str, max_tokens: int = 200) -> Optional[dict]:
        if not image_bytes:
            return None
        b64 = base64.b64encode(image_bytes).decode()
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
    
    # ─── Coordinate mapping ───

    @staticmethod
    def image_to_screen_coords(ix: float, iy: float, geo: dict) -> tuple:
        """Convert Qwen-VL image pixel coords to absolute screen coords.

        Qwen-VL sees the cropped+resized image.  This maps back through
        crop offset + scale factor to the original screen coordinate space.
        """
        scale = geo.get("scale", 1.0) or 1.0
        # ix,iy are in the (possibly resized) crop image space
        # Step 1: scale back to crop-image space
        cx = ix / scale
        cy = iy / scale
        # Step 2: add crop offset → screen space
        sx = cx + geo.get("crop_left", 0)
        sy = cy + geo.get("crop_top", 0)
        return int(sx), int(sy)

    def find_contact_position(self, contact: str) -> Optional[dict]:
        """Screenshot + Qwen-VL: locate contact in search results.

        Returns dict with screen_x, screen_y (click point) + Qwen raw fields,
        or None if contact not found or Qwen unavailable.
        """
        img, geo = self._capture(return_geometry=True)
        if img is None:
            return None
        result = self._ask(img, PROMPT_FIND_CONTACT.replace("__CONTACT__", contact))
        if not result or not result.get("found"):
            return None
        cx = result.get("center_x")
        cy = result.get("center_y")
        if cx is None or cy is None:
            return None
        sx, sy = self.image_to_screen_coords(cx, cy, geo)
        result["screen_x"] = sx
        result["screen_y"] = sy
        return result

    def find_input_position(self) -> Optional[dict]:
        """Screenshot + Qwen-VL: locate chat input box.

        Returns dict with screen_x, screen_y (click point) or None.
        """
        img, geo = self._capture(return_geometry=True)
        if img is None:
            return None
        # Reuse CHAT_OPEN prompt but ask for input position
        prompt = """Analyze this WeChat screenshot. Find the bottom chat message input/composer box.
Return ONLY JSON:
{"input_visible": true/false, "center_x": pixel_x, "center_y": pixel_y, "confidence": 0.0-1.0}"""
        result = self._ask(img, prompt)
        if not result or not result.get("input_visible"):
            return None
        cx = result.get("center_x")
        cy = result.get("center_y")
        if cx is None or cy is None:
            return None
        sx, sy = self.image_to_screen_coords(cx, cy, geo)
        result["screen_x"] = sx
        result["screen_y"] = sy
        return result

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
