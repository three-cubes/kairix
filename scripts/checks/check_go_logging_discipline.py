"""G8: logging via ``log/slog`` only — no ``fmt.Print*`` / ``log.Print*``
in production code.

Structured logs are the operator-facing surface for ops binaries. Mixed
``fmt.Println("foo")`` and ``slog.Info(...)`` calls produce a log stream
that is half-structured-half-prose, which the kairix dogfood agents
can't parse uniformly.

Detection: walk every ``services/*/**/*.go`` excluding ``*_test.go`` and
``cmd/<name>/main.go`` (the entrypoint may emit a final ``fmt.Fprintln``
to stderr after slog flush — accepted boundary use). Flag any file that
calls:

  - ``fmt.Print(...)`` / ``fmt.Println(...)`` / ``fmt.Printf(...)``
  - ``log.Print(...)`` / ``log.Println(...)`` / ``log.Printf(...)``
  - ``log.Fatal(...)`` / ``log.Fatalln(...)`` / ``log.Fatalf(...)``
  - ``log.Panic(...)`` / ``log.Panicln(...)`` / ``log.Panicf(...)``

``fmt.Fprint*`` to an explicit writer (e.g. ``fmt.Fprintf(os.Stdout, …)``)
is allowed — that's CLI output, not logging. ``fmt.Sprintf`` (string
formatting) is allowed.

Baseline: ``.architecture/baseline/go-logging-discipline-files.txt``
ships empty.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES_DIR = REPO_ROOT / "services"

# fmt.Print / fmt.Println / fmt.Printf — but NOT fmt.Fprint* / fmt.Sprint*
_FMT_PRINT_RE = re.compile(r"\bfmt\.Print(?:f|ln)?\s*\(")

# log.Print* / log.Println* / log.Printf / log.Fatal* / log.Panic*
# (note: this targets the *package* log, not log/slog)
_LOG_PRINT_RE = re.compile(r"\blog\.(?:Print(?:f|ln)?|Fatal(?:f|ln)?|Panic(?:f|ln)?)\s*\(")


def _is_exempt(go_file: Path, services_root: Path) -> bool:
    """``*_test.go`` always exempt; ``cmd/<name>/main.go`` exempt for boundary use."""
    if go_file.name.endswith("_test.go"):
        return True
    # services/<name>/cmd/<name>/main.go — entrypoint may need raw fmt.Fprintln
    # to stderr on fatal exit paths (after slog is flushed).
    try:
        rel = go_file.relative_to(services_root).parts
    except ValueError:
        return False
    return len(rel) == 4 and rel[1] == "cmd" and rel[3] == "main.go"


def _file_violates(go_file: Path) -> bool:
    """True if file calls fmt.Print* / log.Print* (after stripping line comments)."""
    text = go_file.read_text(encoding="utf-8")
    stripped_lines = []
    for line in text.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        stripped_lines.append(line)
    body = "\n".join(stripped_lines)
    return bool(_FMT_PRINT_RE.search(body) or _LOG_PRINT_RE.search(body))


REMEDIATION = """Logging must go through ``log/slog`` — no
``fmt.Print*`` or stdlib ``log.Print*`` in production library code.
Refactor to use the structured logger and pass it through the call
chain.

fix: replace fmt/log calls with slog:
  fmt.Println("user logged in: " + uid)
  → logger.Info("user logged in", slog.String("uid", uid))

  log.Printf("retry %d: %v", n, err)
  → logger.Warn("retry", slog.Int("attempt", n), slog.String("err", err.Error()))

  log.Fatalf("...")  // in library code
  → return fmt.Errorf(...) and let main decide

For CLI output written to a specific writer (``fmt.Fprintf(stdout, ...)``)
that's not logging — keep it. Only fmt.Print* / log.Print* calls that
implicitly write to stdout/stderr are flagged.
next: re-run python3 scripts/checks/check_go_logging_discipline.py.
run: bash scripts/checks/run-all.sh

Pass example:
  logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
  logger.Info("server starting", slog.String("addr", addr))

Forbidden example:
  fmt.Println("server starting on", addr)  // implicit stdout, unstructured
  log.Printf("retry %d", n)                // stdlib log, unstructured

Why: kairix dogfood agents parse structured JSON logs uniformly. Mixed
prose/JSON streams break that contract. Net-new violations block.
"""


def collect_violations(services_root: Path = SERVICES_DIR) -> set[Path]:
    """Walk services/**/*.go excluding tests + main; flag fmt/log Print* calls."""
    violations: set[Path] = set()
    if not services_root.is_dir():
        return violations
    for go_file in services_root.rglob("*.go"):
        if _is_exempt(go_file, services_root):
            continue
        try:
            if _file_violates(go_file):
                violations.add(go_file.relative_to(REPO_ROOT))
        except OSError:
            continue
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("go-logging-discipline", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
