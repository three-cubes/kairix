"""Main normalisation pipeline for the kairix reference library.

Orchestrates filtering, cleanup, splitting, frontmatter injection,
deduplication, and catalogue generation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from kairix.knowledge.reflib.catalogue import (
    CatalogueEntry,
    generate_catalogue,
    generate_licence_notices,
)
from kairix.knowledge.reflib.dedup import (
    choose_canonical,
    find_exact_duplicates,
    hash_content,
)
from kairix.knowledge.reflib.filters import filter_collection
from kairix.knowledge.reflib.frontmatter import build_frontmatter, inject_frontmatter
from kairix.knowledge.reflib.markdown import clean_markdown
from kairix.knowledge.reflib.sources import SourceDef, get_source
from kairix.knowledge.reflib.splitter import (
    is_too_small,
    needs_split,
    split_at_headings,
    to_kebab_case,
)

logger = logging.getLogger(__name__)


@dataclass
class NormaliseConfig:
    """Configuration for the normalisation pipeline."""

    input_dir: Path
    output_dir: Path
    max_tier: int = 3
    max_file_size: int = 50_000
    min_file_size: int = 500
    dry_run: bool = False
    dedup: bool = True


@dataclass
class NormaliseReport:
    """Results of a normalisation run."""

    total_input: int = 0
    total_output: int = 0
    filtered_boilerplate: int = 0
    filtered_licence: int = 0
    filtered_too_small: int = 0
    split_count: int = 0
    exact_duplicates: int = 0
    html_cleaned: int = 0
    frontmatter_added: int = 0
    renamed: int = 0
    unregistered_sources: list[str] = field(default_factory=list)
    collections: dict[str, int] = field(default_factory=dict)


def discover_sources(input_dir: Path) -> list[tuple[str, str, Path]]:
    """Walk input_dir and yield (collection, dir_name, source_path) triples."""
    results: list[tuple[str, str, Path]] = []
    for collection_dir in sorted(input_dir.iterdir()):
        if not collection_dir.is_dir():
            continue
        collection = collection_dir.name
        for source_dir in sorted(collection_dir.iterdir()):
            if not source_dir.is_dir():
                # Files at collection root (e.g. family-and-education/montessori-method.md)
                continue
            results.append((collection, source_dir.name, source_dir))

    # Also check for files at collection root level
    for collection_dir in sorted(input_dir.iterdir()):
        if not collection_dir.is_dir():
            continue
        root_files = list(collection_dir.glob("*.md"))
        if root_files:
            results.append((collection_dir.name, "", collection_dir))

    return results


def collect_markdown_files(source_path: Path) -> list[Path]:
    """Find all markdown files in a source directory."""
    return sorted(source_path.rglob("*.md"))


def is_gutenberg_text(source: SourceDef) -> bool:
    """Check if a source contains Project Gutenberg texts."""
    return source.format == "text" and "gutenberg" in source.source_url.lower()


def collect_and_filter_source_files(
    source_path: Path,
    source: SourceDef,
    _config: NormaliseConfig,
    report: NormaliseReport,
) -> list[Path]:
    """Read and filter .md files for a source, updating the report."""
    md_files = collect_markdown_files(source_path)
    report.total_input += len(md_files)
    filtered = filter_collection(md_files, source)
    report.filtered_boilerplate += len(md_files) - len(filtered)
    return filtered


def process_source_collection(
    collection: str,
    dir_name: str,
    source_path: Path,
    config: NormaliseConfig,
    source: SourceDef,
    is_gutenberg: bool,
    report: NormaliseReport,
) -> dict[Path, str]:
    """Read files, filter, clean, inject frontmatter, handle splitting.

    Returns a mapping of output path to content for this source.
    """
    filtered = collect_and_filter_source_files(source_path, source, config, report)
    output_files: dict[Path, str] = {}

    for file_path in filtered:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("Cannot read: %s", file_path)
            continue

        # Clean markdown
        original_len = len(text)
        text = clean_markdown(text, is_gutenberg=is_gutenberg)
        if len(text) != original_len:
            report.html_cleaned += 1

        # Check minimum size (skip for README files — curated lists are valuable)
        is_readme = file_path.name.lower() in ("readme.md", "index.md")
        if not is_readme and is_too_small(text, config.min_file_size):
            report.filtered_too_small += 1
            continue

        # Build frontmatter
        fm = build_frontmatter(file_path, source, text)
        text = inject_frontmatter(text, fm)
        report.frontmatter_added += 1

        # Compute relative output path
        rel = file_path.relative_to(source_path)
        parts = [to_kebab_case(str(p)) if p.suffix else p.name.lower() for p in [Path(seg) for seg in rel.parts]]
        if parts and not parts[-1].endswith(".md"):
            parts[-1] = parts[-1] + ".md"

        if str(rel) != "/".join(parts):
            report.renamed += 1

        # Handle splitting
        if needs_split(text, config.max_file_size):
            stem = Path(parts[-1]).stem
            parent_parts = parts[:-1]
            chunks = split_at_headings(text, stem, config.max_file_size)
            report.split_count += len(chunks) - 1

            for chunk_stem, chunk_text in chunks:
                chunk_filename = f"{chunk_stem}.md"
                out_path = config.output_dir / collection / dir_name / "/".join(parent_parts) / chunk_filename
                output_files[out_path] = chunk_text
        else:
            out_path = config.output_dir / collection / dir_name / "/".join(parts)
            output_files[out_path] = text

    return output_files


def deduplicate_output(
    all_hashed: list[tuple[Path, str]],
    output_files: dict[Path, str],
    config: NormaliseConfig,
    report: NormaliseReport,
) -> None:
    """Find and remove exact duplicates from output_files in-place."""
    if not config.dedup:
        return
    duplicates = find_exact_duplicates(all_hashed)
    for _dup_hash, dup_paths in duplicates.items():
        canonical = choose_canonical(dup_paths)
        for p in dup_paths:
            if p != canonical and p in output_files:
                del output_files[p]
                report.exact_duplicates += 1


def write_output_files(
    config: NormaliseConfig,
    output_files: dict[Path, str],
    catalogue_entries: list[CatalogueEntry],
) -> None:
    """Create output dir, write files, generate CATALOGUE.md and LICENSE-NOTICES.md."""
    config.output_dir.mkdir(parents=True, exist_ok=True)

    for out_path, content in output_files.items():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")

    cat_text = generate_catalogue(catalogue_entries)
    (config.output_dir / "CATALOGUE.md").write_text(cat_text, encoding="utf-8")

    lic_text = generate_licence_notices(catalogue_entries)
    (config.output_dir / "LICENSE-NOTICES.md").write_text(lic_text, encoding="utf-8")


def build_catalogue_entry(
    collection: str,
    _dir_name: str,
    source: SourceDef,
    count: int,
    size: int,
    today: str,
) -> CatalogueEntry:
    """Construct a CatalogueEntry for a processed source."""
    return CatalogueEntry(
        collection=collection,
        source_name=source.name,
        source_url=source.source_url,
        licence=source.licence,
        licence_tier=source.licence_tier,
        file_count=count,
        total_size_kb=size / 1024,
        date_verified=today,
    )


def normalise(config: NormaliseConfig) -> NormaliseReport:
    """Run the full normalisation pipeline.

    1. Discover all sources in input_dir
    2. Match each to a SourceDef from the registry
    3. Filter by licence tier
    4. Filter boilerplate
    5. Clean markdown
    6. Split large files / discard small files
    7. Inject frontmatter
    8. Normalise filenames
    9. Deduplicate
    10. Write output
    11. Generate CATALOGUE.md + LICENSE-NOTICES.md
    """
    report = NormaliseReport()
    catalogue_entries: list[CatalogueEntry] = []

    all_hashed: list[tuple[Path, str]] = []
    output_files: dict[Path, str] = {}

    sources = discover_sources(config.input_dir)
    today = date.today().isoformat()

    for collection, dir_name, source_path in sources:
        source = get_source(collection, dir_name)

        if source is None:
            if dir_name == "":
                continue
            report.unregistered_sources.append(f"{collection}/{dir_name}")
            logger.warning("Unregistered source: %s/%s — skipping", collection, dir_name)
            continue

        if source.licence_tier > config.max_tier:
            md_files = list(source_path.rglob("*.md"))
            report.filtered_licence += len(md_files)
            logger.info(
                "Excluded (tier %d): %s/%s (%d files)",
                source.licence_tier,
                collection,
                dir_name,
                len(md_files),
            )
            continue

        is_gutenberg = is_gutenberg_text(source)
        source_files = process_source_collection(
            collection,
            dir_name,
            source_path,
            config,
            source,
            is_gutenberg,
            report,
        )

        source_output_count = len(source_files)
        source_output_size = sum(len(c.encode("utf-8")) for c in source_files.values())

        for out_path, content in source_files.items():
            content_hash = hash_content(content)
            all_hashed.append((out_path, content_hash))
            output_files[out_path] = content

        report.collections[f"{collection}/{dir_name}"] = source_output_count
        catalogue_entries.append(
            build_catalogue_entry(collection, dir_name, source, source_output_count, source_output_size, today),
        )

    deduplicate_output(all_hashed, output_files, config, report)
    report.total_output = len(output_files)

    if not config.dry_run:
        write_output_files(config, output_files, catalogue_entries)
        logger.info(
            "Normalisation complete: %d → %d files",
            report.total_input,
            report.total_output,
        )
    else:
        logger.info(
            "Dry run: would produce %d files from %d input",
            report.total_output,
            report.total_input,
        )

    return report
