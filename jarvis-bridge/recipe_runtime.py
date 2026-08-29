"""Fail-closed interpreter for registered read-only capability recipes."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class RecipeExecutionError(RuntimeError):
    """Structured recipe failure used to make fallback decisions fail-closed."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        fallback_allowed: bool = False,
    ) -> None:
        self.code = str(code)
        self.fallback_allowed = bool(fallback_allowed)
        super().__init__(message if message is not None else code)


class RecipeRuntime:
    def __init__(
        self,
        *,
        tools: dict[str, Callable[[], Any]],
        templates: dict[str, Callable[[Any], str]],
    ) -> None:
        self.tools = dict(tools)
        self.templates = dict(templates)

    def execute(self, bundle: dict[str, Any]) -> str:
        recipe = bundle.get("recipe") if isinstance(bundle, dict) else None
        if not isinstance(recipe, dict):
            raise RecipeExecutionError("invalid recipe")

        tool_name = str(recipe.get("tool") or "")
        tool = self.tools.get(tool_name)
        if tool is None:
            raise RecipeExecutionError(f"unregistered tool: {tool_name}")
        value = tool()

        for transform in recipe.get("transforms", []):
            if not isinstance(transform, dict) or transform.get("op") != "filter_eq":
                raise RecipeExecutionError("unregistered transform")
            if not isinstance(value, list):
                raise RecipeExecutionError("filter_eq requires a list")
            field = str(transform.get("field") or "")
            expected = transform.get("value")
            value = [
                item for item in value
                if isinstance(item, dict) and item.get(field) == expected
            ]

        template_name = str(recipe.get("response_template") or "")
        template = self.templates.get(template_name)
        if template is None:
            raise RecipeExecutionError(f"unregistered template: {template_name}")
        rendered = template(value)
        if not isinstance(rendered, str) or not rendered.strip():
            raise RecipeExecutionError("template returned an empty response")
        return rendered
