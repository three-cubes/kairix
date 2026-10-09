"""The one LLM-judge call protocol every judge shares.

Every judge (benchmark relevance, conversation suite, unified scorer, LoCoMo
spike) does the same two steps, so they live here once:

* :func:`call_judge` — run the backend call. A raised exception becomes
  ``backend_error`` carrying only the exception class name; a blank reply
  (the ``LLMBackend`` / ``ChatBackend`` failure sentinel) is also
  ``backend_error``.
* :func:`parse_judge_score` — turn the reply into a clamped 0.0-1.0 float.
  The first parseable float wins (surrounding text is tolerated); no float,
  or a non-finite one (``nan`` / ``inf``), is ``unparseable_response``.

A judge failure is never a score: both raise :class:`JudgeFailedError` and
the caller records the case as unscored.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from kairix.quality.scoring.types import (
    JUDGE_FAILURE_BACKEND_ERROR,
    JUDGE_FAILURE_UNPARSEABLE,
    JudgeFailedError,
)


def call_judge(complete: Callable[[], str]) -> str:
    """Run ``complete()`` and return its non-blank reply.

    Raises:
        JudgeFailedError: ``backend_error`` when ``complete`` raises (class
            name only, never the message) or returns a blank reply.
    """
    try:
        reply = complete()
    except Exception as exc:
        raise JudgeFailedError.from_backend_exception(exc) from exc
    if not reply or not reply.strip():
        raise JudgeFailedError(JUDGE_FAILURE_BACKEND_ERROR, "empty judge reply (LLM backend failure)")
    return reply


def parse_judge_score(reply: str) -> float:
    """Parse a judge reply into a 0.0-1.0 score (out-of-range values clamp).

    Raises:
        JudgeFailedError: ``unparseable_response`` when the reply holds no
            parseable float or the float is non-finite. Only the reply's
            length is reported, never its text.
    """
    stripped = (reply or "").strip()
    value = _first_float(stripped)
    if value is None:
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, f"non-numeric reply ({len(stripped)} chars)")
    if not math.isfinite(value):
        raise JudgeFailedError(JUDGE_FAILURE_UNPARSEABLE, "non-finite reply")
    return max(0.0, min(1.0, value))


def _first_float(text: str) -> float | None:
    """The whole text as a float, else its first parseable token, else None."""
    for candidate in (text, *text.replace(",", " ").split()):
        try:
            return float(candidate)
        except ValueError:
            continue
    return None


__all__ = ["call_judge", "parse_judge_score"]
