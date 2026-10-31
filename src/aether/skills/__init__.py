"""Typed skills agents compose instead of hand-coding multi-step logic."""

from aether.skills.base import (
    Skill,
    SkillContext,
    SkillError,
    SkillNotPermitted,
    UnknownSkill,
    execute_skill,
)
from aether.skills.builtin import builtin_skills
from aether.skills.registry import AgentSkills, SkillRegistry

__all__ = [
    "AgentSkills",
    "Skill",
    "SkillContext",
    "SkillError",
    "SkillNotPermitted",
    "SkillRegistry",
    "UnknownSkill",
    "builtin_skills",
    "execute_skill",
]
