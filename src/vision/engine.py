"""Visual Context Engine — main orchestrator for passive screen observation."""

import time, logging, threading
from typing import Optional
from PIL import Image

from src.vision.capture import ScreenCapture, Screenshot, compute_change_ratio
from src.vision.analyzer import VisionAnalyzer
from src.vision.memory import VisualMemory, get_visual_memory
from src.vision.privacy import PrivacyFilter
from src.vision.router import EfficiencyRouter, get_efficiency_router

logger = logging.getLogger("ai_company.vision.engine")

# Default configuration
DEFAULT_CONFIG = {
    "interval_seconds": 30,        # Min interval between captures
    "change_threshold": 0.15,      # 15% pixel change to trigger analysis
    "vision_model": "qwen-vl",       # Default: Qwen-VL (configured via .env)
    "max_quality": 60,             # JPEG quality for API calls
    "privacy_check": True,         # Enable privacy filtering
    "auto_mode": False,            # Background auto-observation
    "session_integration": True,   # Write to session memory
}

class VisualContextEngine:
    """Main engine for passive visual context gathering.
    
    Usage:
        engine = VisualContextEngine(session_id="abc123")
        engine.configure(interval_seconds=30, change_threshold=0.15)
        
        # One-shot scan
        result = engine.scan()
        
        # Auto mode (background thread)
        engine.start_auto()
        ...
        engine.stop_auto()
    """
    
    def __init__(self, session_id: str = "", **kwargs):
        self.config = {**DEFAULT_CONFIG, **kwargs}
        self.session_id = session_id
        self.capture = ScreenCapture()
        self.analyzer = VisionAnalyzer(model_name=self.config["vision_model"])
        self.memory = get_visual_memory(session_id)
        self.privacy = PrivacyFilter()
        self.router = get_efficiency_router()
        
        self._last_screenshot: Optional[Screenshot] = None
        self._auto_thread: Optional[threading.Thread] = None
        self._running = False
        self._scan_count = 0
    
    def configure(self, **kwargs):
        """Update configuration."""
        self.config.update(kwargs)
        if "vision_model" in kwargs:
            self.analyzer = VisionAnalyzer(model_name=kwargs["vision_model"])
    
    def scan(self, force: bool = False) -> Optional[dict]:
        """Perform one visual scan. Returns analysis or None if skipped."""
        # 1. Get active app info via CLI (fast, no vision needed)
        app_info = self.router.get_active_app()
        
        # 2. Capture screenshot
        screenshot = self.capture.capture()
        if screenshot is None:
            logger.warning("Screen capture failed")
            return self._build_result(app_info, None)
        
        # 3. Change detection — skip analysis if screen hasn't changed enough
        if not force and self._last_screenshot is not None:
            try:
                ratio = compute_change_ratio(self._last_screenshot.image, screenshot.image)
                if ratio < self.config["change_threshold"]:
                    logger.debug("Skipping analysis: change ratio %.2f%% < threshold", ratio * 100)
                    return self._build_result(app_info, None, skipped=True, change_ratio=ratio)
            except Exception:
                pass  # Failed change detection → proceed with analysis
        
        self._last_screenshot = screenshot
        
        # 4. Privacy check
        if self.config["privacy_check"]:
            if self.privacy.quick_check(screenshot.image):
                logger.info("Privacy filter triggered — skipping analysis")
                return self._build_result(app_info, None, skipped=True, reason="privacy")
        
        # 5. Vision analysis
        analysis = self.analyzer.analyze(screenshot.image)
        if analysis is None:
            return self._build_result(app_info, None)
        
        # Merge CLI app info with vision analysis
        analysis["app_info"] = app_info
        
        # 6. Store to visual memory
        self.memory.record(analysis, app_info)
        self._scan_count += 1
        
        return self._build_result(app_info, analysis)
    
    def _build_result(self, app_info: dict, analysis: Optional[dict],
                      skipped: bool = False, reason: str = "",
                      change_ratio: float = 0) -> dict:
        result = {
            "timestamp": time.time(),
            "app_info": app_info,
            "analysis": analysis,
            "skipped": skipped,
            "reason": reason,
            "scan_count": self._scan_count,
        }
        if change_ratio:
            result["change_ratio"] = round(change_ratio, 3)
        return result
    
    def get_context(self) -> str:
        """Get visual context for LLM prompt injection."""
        return self.memory.get_context_for_prompt()
    
    def get_status(self) -> dict:
        """Get engine status."""
        current = self.memory.get_current_activity()
        return {
            "running": self._running,
            "auto_mode": self.config["auto_mode"],
            "scan_count": self._scan_count,
            "interval": self.config["interval_seconds"],
            "threshold": self.config["change_threshold"],
            "model": self.config["vision_model"],
            "model_available": self.analyzer.is_available,
            "current_activity": current,
            "timeline": self.memory.get_timeline(30),
        }
    
    def start_auto(self):
        """Start background auto-observation thread."""
        if self._running:
            return
        self._running = True
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self._auto_thread.start()
        logger.info("VCE auto mode started (interval=%ds)", self.config["interval_seconds"])
    
    def stop_auto(self):
        """Stop background auto-observation."""
        self._running = False
        if self._auto_thread:
            self._auto_thread.join(timeout=5)
        logger.info("VCE auto mode stopped (%d scans)", self._scan_count)
    
    def _auto_loop(self):
        """Background loop for periodic scanning."""
        while self._running:
            try:
                self.scan()
            except Exception as e:
                logger.warning("VCE auto scan failed: %s", e)
            
            # Sleep in small increments for responsive shutdown
            for _ in range(int(self.config["interval_seconds"])):
                if not self._running:
                    break
                time.sleep(1)


# Global singleton per session
_engines: dict[str, VisualContextEngine] = {}

def get_visual_engine(session_id: str = "", **kwargs) -> VisualContextEngine:
    global _engines
    key = session_id or "default"
    if key not in _engines:
        _engines[key] = VisualContextEngine(session_id, **kwargs)
    return _engines[key]
