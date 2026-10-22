"""Skill catalogue: manifests in ``skills/<id>/skill.yml``, their prompts and evals.

CLI: ``python -m squadhq.skills validate | list | render-prompt | catalogue-md``.
Evals: ``python -m squadhq.skills.evals run``.
"""
from squadhq.skills.loader import (
    Skill,
    SkillError,
    ValidationReport,
    get_skill,
    load_catalogue,
    validate_catalogue,
)
from squadhq.skills.render import RenderedPrompt, render_prompt

__all__ = [
    "Skill", "SkillError", "ValidationReport", "get_skill", "load_catalogue", "validate_catalogue",
    "RenderedPrompt", "render_prompt",
]
