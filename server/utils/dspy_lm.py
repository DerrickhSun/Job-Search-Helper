"""Shared DSPy language-model configuration for job matching and cover letters."""

import os

import dspy
from dotenv import load_dotenv

load_dotenv()

# Passed through to LiteLLM's completion() call (dspy.LM forwards unknown kwargs there). Without
# this, a stalled network call (dropped connection, provider hiccup) hangs forever with nothing to
# bound it — every LLM call (cover letters, gates_pass/fit_score) shares this one configured LM and
# serializes behind a single lock, so one stuck call blocks the whole pipeline indefinitely, and on
# Windows a KeyboardInterrupt can't even reach a thread parked in a timeout-less blocking socket
# read until that read itself returns. Override via ``DSPY_LM_TIMEOUT_SECONDS`` if needed.
_DEFAULT_LM_TIMEOUT_SECONDS = 90.0


def configure_dspy() -> None:
    """
    Set ``dspy.settings.lm`` using LiteLLM-backed models (same pattern as ``dspy.LM`` in translators).

    Environment:
      - ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY`` (required)
      - ``DSPY_LM`` — optional model id, e.g. ``openai/gpt-4o-mini`` or
        ``anthropic/claude-sonnet-4-20250514``
      - ``DSPY_LM_TIMEOUT_SECONDS`` — optional per-request timeout (default 90s; see
        :data:`_DEFAULT_LM_TIMEOUT_SECONDS`)
    """
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    try:
        timeout = float(os.environ.get("DSPY_LM_TIMEOUT_SECONDS") or _DEFAULT_LM_TIMEOUT_SECONDS)
    except ValueError:
        timeout = _DEFAULT_LM_TIMEOUT_SECONDS

    if openai_key:
        model = os.environ.get("DSPY_LM", "openai/gpt-4o-mini")
        lm = dspy.LM(model, api_key=openai_key, timeout=timeout)
    elif anthropic_key:
        model = os.environ.get("DSPY_LM", "anthropic/claude-sonnet-4-20250514")
        lm = dspy.LM(model, api_key=anthropic_key, timeout=timeout)
    else:
        raise EnvironmentError(
            "Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env for DSPy (job match + cover letter)."
        )

    dspy.configure(lm=lm)
