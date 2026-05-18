"""Security helpers for the eval module — path confinement and prompt-injection
sanitisers.

Phase 0b of #143. Two threats this module addresses:

1. **Path traversal (S2083).** CLI flags and suite-YAML fields can carry
   ``../../etc/passwd`` or other escapes. ``confine_to(root, candidate)``
   resolves ``candidate`` against ``root`` and verifies the result stays
   inside ``root``. Raises ``PathTraversalError`` (a ``ValueError`` subclass)
   on escape.

2. **Prompt injection.** Document content sent to the LLM judge / query
   generator may carry adversarial role-marker tokens (``<|im_start|>``,
   ``<<SYS>>``, ``[INST]`` etc.) that some models honour as control tokens.
   ``sanitise_document_content(text, *, cap)`` strips/escapes those tokens,
   removes newlines, and truncates to ``cap`` characters. Use it at every
   site where untrusted vault content is interpolated into an LLM prompt.

Both helpers are kept tiny and pure so they are trivially auditable. The
documented threat model: vault content is **trusted-but-adversarial** — the
operator controls what's in the corpus, but cannot guarantee no document was
edited by a hostile party. The eval module must defend itself even when the
content is local.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default cap for any single document snippet interpolated into a prompt.
# 1000 chars is long enough to capture relevance signal for the judge,
# short enough to stop unbounded escalation if an adversary stuffs a
# document with role-marker payload.
DEFAULT_PROMPT_SNIPPET_CAP: int = 1000

# Role-marker tokens that some LLMs honour as control sequences. Stripping
# them out prevents an adversarial document from breaking out of the
# delimited ``<document>...</document>`` envelope. Keep this list narrow and
# explicit — broad regex sweeps risk mangling legitimate corpus content.
_ROLE_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>]{0,40}\|>"),  # ChatML / Llama: <|im_start|>, <|im_end|>, <|system|>
    re.compile(r"<<SYS>>|<</SYS>>"),  # Llama-2 system markers
    re.compile(r"\[INST\]|\[/INST\]"),  # Llama-2 instruction markers
    re.compile(r"<\|endoftext\|>"),  # OpenAI legacy EOT
)


class PathTraversalError(ValueError):
    """Raised when a candidate path resolves outside its allowed root.

    Subclass of ``ValueError`` so existing ``except ValueError`` blocks
    around path resolution catch it without code churn, while callers that
    care about the distinction can ``except PathTraversalError``.
    """


def confine_to(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` against ``root`` and verify it stays inside ``root``.

    ``candidate`` may be absolute or relative. Symlinks are followed via
    ``Path.resolve()``, so a symlink inside ``root`` that points outside is
    detected. Raises :class:`PathTraversalError` on escape; the caller
    decides whether to log, abort, or fall back.

    Args:
        root:      The allowed root directory. Must exist for ``.resolve()``
                   to canonicalise correctly; the function does not create it.
        candidate: A user-supplied path string or ``Path`` object.

    Returns:
        The resolved absolute ``Path`` inside ``root``.

    Raises:
        PathTraversalError: when the resolved candidate is not inside
                            the resolved root.
    """
    root_resolved = Path(root).resolve()
    cand = Path(candidate)
    # When candidate is absolute, the / operator returns it unchanged; when
    # relative, it's joined onto root. Either way, ``.resolve()`` then
    # canonicalises so ``..`` segments are collapsed before the check.
    combined = cand if cand.is_absolute() else (root_resolved / cand)
    resolved = combined.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise PathTraversalError(f"Path {str(candidate)!r} escapes allowed root {str(root_resolved)!r}") from e
    return resolved


def sanitise_document_content(text: str, *, cap: int = DEFAULT_PROMPT_SNIPPET_CAP) -> str:
    """Sanitise untrusted document content before interpolation into an LLM prompt.

    Three defences, in order:

    1. Strip role-marker tokens (``<|im_start|>``, ``<<SYS>>``, ``[INST]``,
       etc.) that some models honour as control sequences.
    2. Replace literal newlines / carriage returns with spaces so adversarial
       content cannot break out of a one-line tag envelope.
    3. Truncate to ``cap`` characters to bound the attack surface.

    The output is safe to interpolate inside ``<document>...</document>``
    tags as long as the surrounding system prompt instructs the model to
    treat content inside the tags as data, never instructions (see
    :func:`kairix.quality.eval.judge.LLMJudge._build_prompt`).

    Args:
        text: Raw document snippet from the vault / corpus.
        cap:  Maximum output length in characters. Defaults to
              :data:`DEFAULT_PROMPT_SNIPPET_CAP`.

    Returns:
        Sanitised single-line string of at most ``cap`` characters.
    """
    if not text:
        return ""
    cleaned = text
    for pattern in _ROLE_MARKER_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    return cleaned[:cap]
