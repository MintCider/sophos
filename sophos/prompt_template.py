"""Explicit placeholder rendering for the agent system prompt."""

from __future__ import annotations

RUNTIME_PLACEHOLDERS = frozenset(
    {
        "current_time",
        "timezone",
        "self_user_id",
        "current_user_id",
        "current_user_roles",
        "conversation_id",
        "conversation_kind",
        "master_users",
        "identity_context",
        "message_format",
        "runtime_context",
    }
)


def render_system_prompt(template: str, values: dict[str, str]) -> str:
    """Replace only supported placeholders and preserve unrelated braces verbatim.

    Legacy prompts that contain no runtime placeholder receive the complete runtime
    block at the end. Once a prompt uses any runtime placeholder, its author controls
    placement and the fallback is disabled to avoid duplicate injection.
    """
    manages_runtime_layout = any(f"{{{name}}}" in template for name in RUNTIME_PLACEHOLDERS)
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace(f"{{{name}}}", value)
    if not manages_runtime_layout:
        runtime_context = values.get("runtime_context", "").strip()
        if runtime_context:
            rendered = f"{rendered.rstrip()}\n\n---\n{runtime_context}"
    return rendered
