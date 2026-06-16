"""Workspace — shared per-task memory for all agents."""
from src.workspace.context import TaskContext, is_followup_query

__all__ = ["TaskContext", "is_followup_query"]
