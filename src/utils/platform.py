"""Platform detection and adaptation for cross-platform support.

Handles:
- OS detection (Darwin/Linux/Windows)
- Python command resolution
- Tool availability checking
- CJK readline compatibility (gnureadline on macOS)
"""

import os
import sys
import platform
import subprocess
import shutil
from dataclasses import dataclass


@dataclass
class PlatformInfo:
    system: str          # "Darwin", "Linux", "Windows"
    arch: str            # "arm64", "x86_64"
    python_cmd: str      # "python3" or "python"
    python_version: str  # "3.14.2"
    shell: str           # "zsh", "bash", "cmd"
    has_ruff: bool
    has_docker: bool
    has_git: bool


def detect() -> PlatformInfo:
    """Detect current platform capabilities."""
    system = platform.system()
    arch = platform.machine()

    # Python command
    python_cmd = "python3" if shutil.which("python3") else "python"

    # Tool availability
    has_ruff = shutil.which("ruff") is not None
    has_docker = shutil.which("docker") is not None
    has_git = shutil.which("git") is not None

    # Shell
    shell = os.environ.get("SHELL", "").split("/")[-1] or (
        "cmd" if system == "Windows" else "sh"
    )

    return PlatformInfo(
        system=system,
        arch=arch,
        python_cmd=python_cmd,
        python_version=platform.python_version(),
        shell=shell,
        has_ruff=has_ruff,
        has_docker=has_docker,
        has_git=has_git,
    )


def setup_readline():
    """Configure readline for CJK multi-byte support.
    
    macOS uses libedit which has CJK editing bugs.
    Linux/Windows use GNU readline by default (no issue).
    """
    import locale
    locale.setlocale(locale.LC_ALL, '')

    if platform.system() == "Darwin":
        try:
            import gnureadline as readline
            return True  # GNU readline loaded on macOS
        except ImportError:
            try:
                import readline
                # libedit on macOS – CJK backspace may still have issues
                return False
            except ImportError:
                return False
    else:
        try:
            import readline
            return True  # GNU readline on Linux
        except ImportError:
            return False


def get_python_cmd() -> str:
    """Get the appropriate Python command for this platform."""
    return "python3" if shutil.which("python3") else "python"


def get_tool_cmd(tool: str) -> str | None:
    """Resolve a tool command (platform-aware). Returns None if unavailable."""
    # Platform-specific overrides
    if platform.system() == "Windows":
        mapping = {"python3": "python", "ruff": "ruff.exe"}
        tool = mapping.get(tool, tool)
    return tool if shutil.which(tool) else None


# Global detection
_platform_info: PlatformInfo | None = None


def get_platform() -> PlatformInfo:
    global _platform_info
    if _platform_info is None:
        _platform_info = detect()
    return _platform_info
