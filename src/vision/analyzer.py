"""Vision Analyzer — send screenshots to vision LLM for understanding."""

import json, logging
from typing import Optional
from PIL import Image

logger = logging.getLogger("ai_company.vision")

# Vision model configurations
VISION_MODELS = {
    "gpt-4o": {
        "provider": "openai",
        "model": "gpt-4o",
        "max_tokens": 300,
        "description": "OpenAI GPT-4o vision",
    },
    "gemini": {
        "provider": "google",
        "model": "gemini-1.5-flash",
        "max_tokens": 300,
        "description": "Google Gemini vision",
    },
    "claude": {
        "provider": "anthropic",
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 300,
        "description": "Anthropic Claude vision",
    },
    "qwen-vl": {
        "provider": "openai_compat",
        "model": "qwen-vl-max",
        "max_tokens": 300,
        "description": "Qwen-VL vision model",
    },
    "deepseek": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 300,
        "description": "DeepSeek (vision NOT yet supported — placeholder)",
        "vision_supported": False,
    },
}

VISION_ANALYSIS_PROMPT = """Analyze this screenshot. Return ONLY a JSON object:

{
  "apps": [{"name": "Cursor", "confidence": 0.95}],
  "active_app": "Cursor",
  "activity": "Writing Python code",
  "topic": "AI agent development",
  "summary": "User is editing Python code in Cursor IDE"
}

Rules:
- Identify ALL visible applications by their UI patterns
- active_app: the app in the foreground
- activity: what the user appears to be doing (coding, reading, chatting, browsing, etc.)
- topic: the general subject if discernible
- summary: one sentence describing the overall screen state
- Keep it concise — only return JSON"""

class VisionAnalyzer:
    """Analyze screenshots using vision-capable LLMs."""
    
    def __init__(self, model_name: str = "qwen-vl"):
        self.model_name = model_name
        self.model_config = VISION_MODELS.get(model_name, VISION_MODELS["gpt-4o"])
        self._client = None
    
    @property
    def is_available(self) -> bool:
        """Check if this vision model is configured and available."""
        if self.model_config.get("vision_supported") is False:
            return False
        # Check for API key
        if self.model_config["provider"] == "openai":
            import os
            return bool(os.environ.get("OPENAI_API_KEY"))
        if self.model_config["provider"] == "anthropic":
            import os
            return bool(os.environ.get("ANTHROPIC_API_KEY"))
        if self.model_config["provider"] == "google":
            import os
            return bool(os.environ.get("GOOGLE_API_KEY"))
        if self.model_config["provider"] == "openai_compat":
            import os
            return bool(os.environ.get("QWEN_API_KEY"))
        return False
    
    def analyze(self, image: Image.Image) -> Optional[dict]:
        """Analyze a screenshot and return structured understanding."""
        if not self.is_available:
            if self.model_name == "deepseek":
                logger.info("DeepSeek vision not yet available — using fallback")
                return self._fallback_analysis(image)
            return None
        
        try:
            result = self._call_vision_api(image)
            if result:
                return result
        except Exception as e:
            logger.warning("Vision API call failed: %s", e)
        
        return self._fallback_analysis(image)
    
    def _call_vision_api(self, image: Image.Image) -> Optional[dict]:
        provider = self.model_config["provider"]
        
        if provider == "openai":
            return self._call_openai(image)
        elif provider == "anthropic":
            return self._call_anthropic(image)
        elif provider == "google":
            return self._call_google(image)
        elif provider == "openai_compat":
            return self._call_openai_compat(image)
        
        return None
    
    def _call_openai(self, image: Image.Image) -> Optional[dict]:
        try:
            from openai import OpenAI
            client = OpenAI()
            b64 = self._image_to_base64(image)
            response = client.chat.completions.create(
                model=self.model_config["model"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]
                }],
                max_tokens=self.model_config["max_tokens"],
            )
            return self._parse_response(response.choices[0].message.content)
        except ImportError:
            logger.warning("openai package not installed")
            return None
        except Exception as e:
            logger.warning("OpenAI vision failed: %s", e)
            return None
    
    def _call_anthropic(self, image: Image.Image) -> Optional[dict]:
        try:
            import anthropic
            client = anthropic.Anthropic()
            b64 = self._image_to_base64(image)
            response = client.messages.create(
                model=self.model_config["model"],
                max_tokens=self.model_config["max_tokens"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_ANALYSIS_PROMPT},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    ]
                }],
            )
            return self._parse_response(response.content[0].text)
        except ImportError:
            logger.warning("anthropic package not installed")
            return None
        except Exception as e:
            logger.warning("Anthropic vision failed: %s", e)
            return None
    
    def _call_google(self, image: Image.Image) -> Optional[dict]:
        return None  # Placeholder
    
    def _call_openai_compat(self, image: Image.Image) -> Optional[dict]:
        """Qwen-VL via DashScope OpenAI-compatible endpoint."""
        try:
            from openai import OpenAI
            import os
            
            api_key = os.environ.get("QWEN_API_KEY", "")
            base_url = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            model = os.environ.get("QWEN_VISION_MODEL", self.model_config["model"])
            
            client = OpenAI(api_key=api_key, base_url=base_url)
            b64 = self._image_to_base64(image)
            response = client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]
                }],
                max_tokens=self.model_config["max_tokens"],
            )
            return self._parse_response(response.choices[0].message.content)
        except ImportError:
            logger.warning("openai package not installed for Qwen vision")
            return None
        except Exception as e:
            logger.warning("Qwen vision failed: %s", e)
            return None
    
    def _fallback_analysis(self, image: Image.Image) -> dict:
        """Fallback: basic image analysis without vision LLM."""
        w, h = image.size
        # Sample pixels for basic color analysis
        try:
            # Check if mostly dark (terminal/IDE)
            region = image.crop((0, 0, min(w, 100), min(h, 100)))
            pixels = list(region.getdata())
            dark_count = sum(1 for p in pixels if (isinstance(p, int) and p < 50) or (isinstance(p, tuple) and sum(p[:3])/3 < 50))
            is_dark = dark_count / len(pixels) > 0.5 if pixels else False
        except Exception:
            is_dark = False
        
        return {
            "apps": [],
            "active_app": "unknown",
            "activity": "unknown",
            "topic": "",
            "summary": f"Fallback analysis: {w}x{h}, {'dark' if is_dark else 'light'} screen",
            "method": "fallback",
        }
    
    @staticmethod
    def _image_to_base64(image: Image.Image, quality: int = 60) -> str:
        import base64
        from io import BytesIO
        # Convert RGBA to RGB for JPEG compatibility
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        # Resize for efficiency (max 1280px wide)
        w, h = image.size
        if w > 1280:
            ratio = 1280 / w
            image = image.resize((1280, int(h * ratio)), Image.LANCZOS)
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    
    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        if not text:
            return None
        text = text.strip()
        # Extract JSON from markdown fences or raw
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
