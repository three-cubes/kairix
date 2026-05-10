"""Merge two Cobertura coverage.xml reports by per-line union.

Used by F9 (union coverage floor) when a single ``coverage combine`` is
unreliable (e.g. when GitHub Actions' upload-artifact strips dotfiles
or when pytest-cov's data-file behaviour differs across pytest
invocations).

Reads ``unit_xml`` and ``integration_xml`` and writes a merged XML where
each ``<class>`` element's line-rate is the per-line union: a line is
hit if EITHER input report shows ``hits > 0``. Files present in only one
input are kept as-is.

Usage:
    python3 scripts/checks/merge_coverage_xml.py \\
        coverage.xml coverage-integration.xml coverage-union.xml
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def _parse_class_lines(cls: ET.Element) -> dict[int, int]:
    """Return {line_number: hits} for a <class> element."""
    out: dict[int, int] = {}
    for line in cls.iter("line"):
        try:
            n = int(line.get("number", "0"))
            h = int(line.get("hits", "0"))
        except ValueError:
            continue
        out[n] = max(out.get(n, 0), h)
    return out


def _set_class_lines(cls: ET.Element, hit_lines: dict[int, int]) -> None:
    """Replace the <lines> block on a <class> with the merged data and
    recompute line-rate. Also recompute the class's @lines-covered /
    @lines-valid attributes for downstream consumers.
    """
    lines_el = cls.find("lines")
    if lines_el is None:
        lines_el = ET.SubElement(cls, "lines")
    else:
        for child in list(lines_el):
            lines_el.remove(child)

    valid = len(hit_lines)
    covered = sum(1 for h in hit_lines.values() if h > 0)
    rate = (covered / valid) if valid else 1.0

    for n in sorted(hit_lines):
        attrs = {"number": str(n), "hits": str(hit_lines[n])}
        ET.SubElement(lines_el, "line", attrs)

    cls.set("line-rate", f"{rate:.4f}")


def merge(unit_xml: Path, integration_xml: Path, output_xml: Path) -> None:
    unit_tree = ET.parse(unit_xml)
    integ_tree = ET.parse(integration_xml)

    # Build a lookup of integration classes by filename so we can match
    # them against unit classes during the per-class merge below.
    integ_classes: dict[str, ET.Element] = {}
    for cls in integ_tree.iter("class"):
        fname = cls.get("filename") or ""
        if fname:
            integ_classes[fname] = cls

    seen_filenames: set[str] = set()

    for cls in unit_tree.iter("class"):
        fname = cls.get("filename") or ""
        if not fname:
            continue
        seen_filenames.add(fname)
        unit_lines = _parse_class_lines(cls)
        integ_cls = integ_classes.get(fname)
        if integ_cls is not None:
            integ_lines = _parse_class_lines(integ_cls)
            merged: dict[int, int] = {}
            for n in set(unit_lines) | set(integ_lines):
                merged[n] = max(unit_lines.get(n, 0), integ_lines.get(n, 0))
            _set_class_lines(cls, merged)
        else:
            _set_class_lines(cls, unit_lines)

    # Files present only in integration coverage need to be appended into
    # the unit tree under their package element. Find or create
    # <packages>/<package>/<classes> nodes as required.
    unit_root = unit_tree.getroot()
    packages_el = unit_root.find("packages")
    if packages_el is None:
        packages_el = ET.SubElement(unit_root, "packages")

    # Build a lookup of unit packages by name to graft new classes into.
    unit_packages: dict[str, ET.Element] = {}
    for pkg in packages_el.iter("package"):
        unit_packages[pkg.get("name", "")] = pkg

    for fname, integ_cls in integ_classes.items():
        if fname in seen_filenames:
            continue
        # Locate which integration package owns this class. integ_tree
        # has <packages>/<package>/<classes>/<class>.
        owning_pkg_name = ""
        for pkg in integ_tree.iter("package"):
            for c in pkg.iter("class"):
                if c.get("filename") == fname:
                    owning_pkg_name = pkg.get("name", "")
                    break
            if owning_pkg_name:
                break

        target_pkg = unit_packages.get(owning_pkg_name)
        if target_pkg is None:
            target_pkg = ET.SubElement(packages_el, "package", {"name": owning_pkg_name})
            unit_packages[owning_pkg_name] = target_pkg

        target_classes = target_pkg.find("classes")
        if target_classes is None:
            target_classes = ET.SubElement(target_pkg, "classes")

        # Re-set the integration class's lines (recompute its line-rate)
        # and append it to the target package.
        _set_class_lines(integ_cls, _parse_class_lines(integ_cls))
        target_classes.append(integ_cls)

    unit_tree.write(output_xml, encoding="utf-8", xml_declaration=True)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "Usage: merge_coverage_xml.py <unit.xml> <integration.xml> <output.xml>",
            file=sys.stderr,
        )
        return 2
    unit_xml = Path(argv[1])
    integration_xml = Path(argv[2])
    output_xml = Path(argv[3])
    if not unit_xml.exists():
        print(f"ERROR: {unit_xml} not found", file=sys.stderr)
        return 2
    if not integration_xml.exists():
        print(f"ERROR: {integration_xml} not found", file=sys.stderr)
        return 2
    merge(unit_xml, integration_xml, output_xml)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
