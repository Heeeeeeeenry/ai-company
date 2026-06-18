"""Capability Layer — dynamic capability discovery and planning.

Compatibility notes (P0.2 transition):
  - planner.py uses "research" internally; the registry now includes a
    "research" alias that maps to the same tools/agent as "web_search".
    This prevents silent tool loss when old code calls
    resolve_tools(["research", ...]).
  - The canonical capability name is "web_search".  New code should use
    "web_search" directly.  The "research" alias exists only for backward
    compatibility with PATTERN_CAPABILITY_MAP in planner.py.
  - TODO(post-transition): remove the "research" alias once planner.py is
    migrated to use "web_search" in all PATTERN_CAPABILITY_MAP entries.
"""
from src.capability.planner import CapabilityPlanner, CapabilityPlan, resolve_tools as _resolve_tools_legacy
from src.capability.registry import (
    Capability,
    CapabilityRegistry,
    DEFAULT_CAPABILITIES,
    INTENT_CAPABILITY_MAP,
    capability_registry,
    get_capability_registry,
    resolve_tools,
)

# resolve_tools from registry takes precedence; the legacy one from planner
# is available as _resolve_tools_legacy for backward-compat if needed.

__all__ = [
    # Planner (existing)
    "CapabilityPlanner",
    "CapabilityPlan",
    # Registry (new — P0.2)
    "Capability",
    "CapabilityRegistry",
    "DEFAULT_CAPABILITIES",
    "INTENT_CAPABILITY_MAP",
    "capability_registry",
    "get_capability_registry",
    "resolve_tools",
]
