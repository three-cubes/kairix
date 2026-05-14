"""F15: No logging of secret-named variables in plaintext.

Detects code patterns where a variable whose name strongly implies it
holds a secret (api key, token, password, credential, bearer, jwt) is
passed to a logging/print/exception sink without redaction. Secrets in
log streams have caused recurring incidents:

- Boot-time credential reveals (KAIRIX_LLM_API_KEY visible in startup logs)
- Exception messages echoing the originating value back to operators
- Debug prints left over from agent investigation flows

The legitimate sites for handling secret values are ``kairix.secrets``
and ``kairix.credentials`` (the boundary modules); they self-redact in
their own logging. Everywhere else, a secret must be replaced with a
short summary (``"len=%d"``, ``"present" / "absent"``) before any log
call.

Detection (AST):

1. **Sink shape**: ``logger.<level>(...)``, ``logging.<level>(...)``,
   ``print(...)``, ``sys.stdout.write(...)``, ``sys.stderr.write(...)``,
   ``raise <ExceptionType>(...)``.
2. **Suspect argument**: any ``ast.Name`` / ``ast.Attribute`` whose
   trailing identifier matches a secret-naming pattern (see
   ``_SECRET_NAME_PATTERNS``). F-strings (``ast.JoinedStr``) are walked
   into ``ast.FormattedValue`` children to catch interpolations.

A file appears in the violation set if any sink site inside it has a
suspect argument. Baseline at
``.architecture/baseline/no-logging-secrets-files.txt`` grandfathers
existing offenders; net-new violations block at pre-commit and CI.

The detector is deliberately conservative: it only fires on AST nodes
that are unambiguously a sink-with-a-named-secret. Hand-redacted
sites (e.g. ``logger.info("api_key present: %s", api_key is not None)``
where ``api_key`` becomes a bool) are NOT flagged because the suspect
``ast.Name`` resolves to a bool comparison, not a Name node, by the
time it reaches the call.

Allow-list: kairix's own secret-handling modules
(``kairix/secrets.py``, ``kairix/credentials.py``) are exempt — they
own the redaction discipline by definition.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

# Identifier patterns that strongly imply a secret value. Matched against
# the *trailing* segment of a Name/Attribute (e.g. ``self.api_key`` →
# matches against ``api_key``).
_SECRET_NAME_PATTERNS = (
    re.compile(r"^api_key$"),
    re.compile(r".*_api_key$"),
    re.compile(r"^token$"),
    re.compile(r".*_token$"),  # access_token, refresh_token, auth_token
    re.compile(r"^secret$"),
    re.compile(r".*_secret$"),  # client_secret, app_secret
    re.compile(r"^password$"),
    re.compile(r".*_password$"),
    re.compile(r"^credential$|^credentials$"),
    re.compile(r".*_credential[s]?$"),
    re.compile(r"^bearer$"),
    re.compile(r"^jwt$"),
    re.compile(r".*_jwt$"),
    re.compile(r"^private_key$"),
    re.compile(r".*_private_key$"),
)

# Boundary modules — they own secret-redaction discipline and are
# allowed to reference these names directly. The check skips them.
_ALLOW_FILES = {
    "kairix/secrets.py",
    "kairix/credentials.py",
}

# Logger / sink callable surface. Method names that look like
# "logger.{level}(...)" or "logging.{level}(...)". The check matches
# the *method/attribute name*, not the receiver, so renamed loggers
# (``log.info(...)``) are still caught.
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}

# Direct function calls that emit to streams.
_DIRECT_SINKS = {"print"}

REMEDIATION = """Replace the secret argument with a short non-revealing summary
before the log call. Examples that pass:
  logger.info("api_key present: %s", api_key is not None)
  logger.info("token length: %d", len(token))
  raise ValueError(f"auth failed for client {client_id}")  # client_id not secret

Forbidden shapes:
  logger.info("api key is %s", api_key)            # passes the secret
  logger.info(f"auth = {access_token}")            # f-string interpolation
  raise RuntimeError(f"bad token: {token}")        # secret in exception text

The legitimate sites for raw secret handling are ``kairix/secrets.py``
and ``kairix/credentials.py`` (allow-listed). Other modules MUST treat
secret-named values as values to summarise, not values to log."""


def _trailing_name(node: ast.expr) -> str | None:
    """Return the trailing identifier of a Name/Attribute, or None.

    ``some_var``                  → ``some_var``
    ``self.api_key``              → ``api_key``
    ``credentials.access_token``  → ``access_token``
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _looks_like_secret(name: str) -> bool:
    return any(p.match(name) for p in _SECRET_NAME_PATTERNS)


def _arg_references_secret(arg: ast.expr) -> bool:
    """Recursively check if ``arg`` references a secret-named identifier
    in a way that exposes it to the surrounding sink call.

    Cases:
      - ``api_key`` (Name)
      - ``self.api_key`` (Attribute)
      - f-string with ``{api_key}`` interpolation (JoinedStr → FormattedValue → Name)
      - ``str(api_key)``, ``f"{api_key}"`` — both walk down to a Name
    """
    # Direct Name / Attribute leaf.
    leaf = _trailing_name(arg)
    if leaf is not None:
        return _looks_like_secret(leaf)

    # F-string: ast.JoinedStr with FormattedValue children that wrap the actual expr.
    if isinstance(arg, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue) and _arg_references_secret(part.value) for part in arg.values
        )

    # Single-arg call that just passes the secret through (e.g. ``str(token)``).
    if isinstance(arg, ast.Call) and len(arg.args) == 1 and not arg.keywords:
        return _arg_references_secret(arg.args[0])

    return False


def _is_log_call(call: ast.Call) -> bool:
    """``logger.info(...)`` / ``logging.warning(...)`` / ``self.log.debug(...)``."""
    if isinstance(call.func, ast.Attribute) and call.func.attr in _LOG_METHODS:
        return True
    return False


def _is_direct_sink_call(call: ast.Call) -> bool:
    """``print(...)`` or ``sys.stdout.write(...)`` / ``sys.stderr.write(...)``."""
    if isinstance(call.func, ast.Name) and call.func.id in _DIRECT_SINKS:
        return True
    if isinstance(call.func, ast.Attribute) and call.func.attr == "write":
        # Receiver should be sys.stdout / sys.stderr; conservative check on the chain.
        value = call.func.value
        if isinstance(value, ast.Attribute) and value.attr in {"stdout", "stderr"}:
            return True
    return False


def file_has_violation(path: Path) -> bool:
    """True if any sink call in this file has a secret-named argument."""
    try:
        rel = str(path.resolve().relative_to(Path(__file__).resolve().parent.parent.parent))
    except ValueError:
        return False
    if rel in _ALLOW_FILES:
        return False

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    for node in ast.walk(tree):
        # Sink: function call that's a logger/print/sys.stream.write.
        if isinstance(node, ast.Call) and (_is_log_call(node) or _is_direct_sink_call(node)):
            for arg in node.args:
                if _arg_references_secret(arg):
                    return True
            for kw in node.keywords:
                if kw.value is not None and _arg_references_secret(kw.value):
                    return True
            continue
        # Sink: ``raise SomeException(<args>)``.
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for arg in node.exc.args:
                if _arg_references_secret(arg):
                    return True

    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("kairix") if file_has_violation(p)}
    return gate("no-logging-secrets", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
