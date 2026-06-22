"""Learning — automatic skill capture and reuse."""
from src.learning.skill_library import SkillLibrary, Skill, skill_library
from src.learning.auto_discovery import AutoSkillDiscovery, auto_discovery, is_capturable

__all__ = ["SkillLibrary", "Skill", "skill_library", "AutoSkillDiscovery", "auto_discovery", "is_capturable"]
