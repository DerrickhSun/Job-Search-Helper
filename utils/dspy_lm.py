"""Shared DSPy language-model configuration for job matching and cover letters."""

import os

import dspy
from dotenv import load_dotenv

load_dotenv()


def configure_dspy() -> None:
    """
    Set ``dspy.settings.lm`` using LiteLLM-backed models (same pattern as ``dspy.LM`` in translators).

    Environment:
      - ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY`` (required)
      - ``DSPY_LM`` — optional model id, e.g. ``openai/gpt-4o-mini`` or
        ``anthropic/claude-sonnet-4-20250514``
    """
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if openai_key:
        model = os.environ.get("DSPY_LM", "openai/gpt-4o-mini")
        lm = dspy.LM(model, api_key=openai_key)
    elif anthropic_key:
        model = os.environ.get("DSPY_LM", "anthropic/claude-sonnet-4-20250514")
        lm = dspy.LM(model, api_key=anthropic_key)
    else:
        raise EnvironmentError(
            "Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env for DSPy (job match + cover letter)."
        )

    dspy.configure(lm=lm)
