"""Screen Capture — multi-platform, low-frequency, passive observation."""

import os, sys, subprocess, hashlib, tempfile, time
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass
from PIL import Image

@dataclass
class Screenshot:
    image: Image.Image
    timestamp: float
    platform: str
    width: int
    height: int
    file_hash: str = ""
    
    def to_bytes(self, quality: int = 70) -> bytes:
        buf = BytesIO()
        self.image.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    
    def to_base64(self, quality: int = 70) -> str:
        import base64
        return base64.b64encode(self.to_bytes(quality)).decode()

class ScreenCapture:
    """Cross-platform screen capture."""
    
    def __init__(self):
        self.platform = sys.platform  # 'darwin', 'win32', 'linux'
        self._last_hash = ""
    
    def capture(self) -> Optional[Screenshot]:
        """Capture current screen. Returns Screenshot or None on failure."""
        img = self._capture_raw()
        if img is None:
            return None
        
        # Compute hash for change detection
        img_bytes = img.tobytes()
        file_hash = hashlib.md5(img_bytes).hexdigest()
        
        self._last_hash = file_hash
        
        return Screenshot(
            image=img,
            timestamp=time.time(),
            platform=self.platform,
            width=img.width,
            height=img.height,
            file_hash=file_hash,
        )
    
    def _capture_raw(self) -> Optional[Image.Image]:
        if self.platform == "darwin":
            return self._capture_macos()
        elif self.platform == "win32":
            return self._capture_windows()
        else:
            return self._capture_linux()
    
    def _capture_macos(self) -> Optional[Image.Image]:
        """Use macOS screencapture CLI (fast, native)."""
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            subprocess.run(
                ["screencapture", "-x", "-r", path],
                capture_output=True, timeout=5,
            )
            img = Image.open(path)
            img.load()
            return img
        except Exception:
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    
    def _capture_windows(self) -> Optional[Image.Image]:
        try:
            import mss
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                sct_img = sct.grab(monitor)
                return Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        except ImportError:
            return None
    
    def _capture_linux(self) -> Optional[Image.Image]:
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            for cmd in [["import", "-window", "root", path], ["gnome-screenshot", "-f", path]]:
                try:
                    subprocess.run(cmd, capture_output=True, timeout=5)
                    if os.path.exists(path) and os.path.getsize(path) > 0:
                        img = Image.open(path)
                        img.load()
                        return img
                except Exception:
                    continue
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    
    def get_active_window_info(self) -> dict:
        """Get active window info via platform-specific CLI."""
        if self.platform == "darwin":
            return self._active_window_macos()
        elif self.platform == "win32":
            return self._active_window_windows()
        else:
            return self._active_window_linux()
    
    def _active_window_macos(self) -> dict:
        try:
            script = 'tell application "System Events" to get name of first application process whose frontmost is true'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
            app = result.stdout.strip()
            
            # Get window title
            script2 = 'tell application "System Events" to get name of front window of first application process whose frontmost is true'
            result2 = subprocess.run(["osascript", "-e", script2], capture_output=True, text=True, timeout=3)
            title = result2.stdout.strip()
            
            return {"app": app, "title": title, "method": "osascript"}
        except Exception:
            return {"app": "unknown", "title": "", "method": "failed"}
    
    def _active_window_windows(self) -> dict:
        return {"app": "unknown", "title": "", "method": "not_implemented"}
    
    def _active_window_linux(self) -> dict:
        try:
            result = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=3)
            return {"app": "unknown", "title": result.stdout.strip(), "method": "xdotool"}
        except Exception:
            return {"app": "unknown", "title": "", "method": "failed"}


def compute_change_ratio(img1: Image.Image, img2: Image.Image, sample_size: int = 200) -> float:
    """Quick change detection using sampled pixel comparison."""
    if img1.size != img2.size:
        return 1.0
    w, h = img1.size
    step_w = max(w // sample_size, 1)
    step_h = max(h // sample_size, 1)
    total = 0
    changed = 0
    for y in range(0, h, step_h):
        for x in range(0, w, step_w):
            p1 = img1.getpixel((x, y))
            p2 = img2.getpixel((x, y))
            if isinstance(p1, int):
                if abs(p1 - p2) > 30:
                    changed += 1
            else:
                if sum(abs(a - b) for a, b in zip(p1[:3], p2[:3])) > 60:
                    changed += 1
            total += 1
    return changed / max(total, 1)
