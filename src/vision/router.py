"""Efficiency Router — choose the fastest method for a given information need.

Prefer CLI/API/MCP over vision when possible.
Examples:
- "What apps are running?" → pgrep (CLI) → 0.1s
- "What's the active window?" → osascript (CLI) → 0.2s
- "What is the user working on?" → vision (LLM) → 2-5s
"""

import subprocess, sys
from typing import Optional


class EfficiencyRouter:
    """Route information queries to the fastest available method."""
    
    # Mapping: query type → [preferred method, fallback]
    METHODS = {
        "running_apps": ["cli", "vision"],
        "active_app": ["cli", "vision"],
        "active_window_title": ["cli", "vision"],
        "user_activity": ["vision"],  # Only vision can determine this
        "screen_content": ["vision"],  # Only vision
        "browser_url": ["cli", "vision"],  # AppleScript can get Chrome URL
    }
    
    def get_running_apps(self) -> dict:
        """Get list of running applications via CLI first."""
        if sys.platform == "darwin":
            return self._running_apps_macos()
        return {"method": "not_available", "apps": []}
    
    def _running_apps_macos(self) -> dict:
        try:
            # Use lsappinfo for fast app listing
            result = subprocess.run(
                ["lsappinfo", "list"],
                capture_output=True, text=True, timeout=3
            )
            # Parse: each line has app name
            apps = []
            for line in result.stdout.split("\n"):
                if '"' in line:
                    # Extract app name from quotes
                    parts = line.split('"')
                    if len(parts) >= 2:
                        apps.append(parts[1])
            return {"method": "cli", "apps": list(set(apps))[:30]}
        except Exception:
            # Fallback: ps aux
            try:
                result = subprocess.run(
                    ["ps", "aux"],
                    capture_output=True, text=True, timeout=3
                )
                apps = []
                for line in result.stdout.split("\n"):
                    if ".app/Contents/MacOS/" in line:
                        app = line.split(".app/")[0].split("/")[-1]
                        apps.append(app)
                return {"method": "cli_ps", "apps": list(set(apps))[:30]}
            except Exception:
                return {"method": "failed", "apps": []}
    
    def get_active_app(self) -> dict:
        """Get active application info via CLI."""
        if sys.platform == "darwin":
            try:
                script = 'tell application "System Events" to get name of first application process whose frontmost is true'
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=3
                )
                app = result.stdout.strip()
                
                # Also get window title
                script2 = 'tell application "System Events" to get name of front window of first application process whose frontmost is true'
                result2 = subprocess.run(
                    ["osascript", "-e", script2],
                    capture_output=True, text=True, timeout=3
                )
                title = result2.stdout.strip()
                
                return {
                    "method": "cli",
                    "app": app,
                    "title": title,
                }
            except Exception:
                pass
        return {"method": "failed", "app": "unknown", "title": ""}
    
    def should_use_vision(self, query_type: str) -> bool:
        """Determine if vision is needed for this query type."""
        methods = self.METHODS.get(query_type, ["vision"])
        return methods[0] == "vision"


# Global singleton
_router: Optional[EfficiencyRouter] = None

def get_efficiency_router() -> EfficiencyRouter:
    global _router
    if _router is None:
        _router = EfficiencyRouter()
    return _router
