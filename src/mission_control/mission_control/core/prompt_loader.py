"""
PromptLoader — renders prompt templates from .md files with {variable} substitution.

Templates live in the ``prompts/`` directory next to the package root.
A template name like ``"content_research"`` maps to ``prompts/content_research.md``.

Usage:
    loader = PromptLoader()
    text = loader.render("content_research", task_title="My Title", context_data="...")
"""

import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# Locate prompts/ directory relative to the package
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


class PromptLoader:
    """Load and render prompt templates from .md files."""

    def __init__(self, prompts_dir: Path | str | None = None):
        self.prompts_dir = Path(prompts_dir) if prompts_dir else _PROMPTS_DIR
        self._cache: dict[str, str] = {}

    def _load(self, name: str) -> str:
        """Load a template file by name, with caching."""
        if name in self._cache:
            return self._cache[name]
        path = self.prompts_dir / f"{name}.md"
        if not path.exists():
            logger.warning("Prompt template not found", name=name, path=str(path))
            return ""
        text = path.read_text(encoding="utf-8")
        self._cache[name] = text
        return text

    def render(self, name: str, **variables: Any) -> str:
        """Load template ``name`` and substitute {variable} placeholders.

        Variables can also be passed as a dict via ``variables["vars"]``.
        """
        template = self._load(name)
        if not template:
            return ""
        result = template
        for key, value in variables.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result

    def render_composite(self, names: list[str], **variables: Any) -> str:
        """Render multiple templates and concatenate them."""
        parts = []
        for name in names:
            rendered = self.render(name, **variables)
            if rendered:
                parts.append(rendered)
        return "\n\n".join(parts)
