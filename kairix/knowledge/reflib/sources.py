"""Source registry for the kairix reference library.

Single source of truth mapping every sub-source to its metadata.
The normalisation pipeline, frontmatter generator, and licence filter
all read from this registry.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Licence constants (SPDX identifiers used across multiple source definitions)
# ---------------------------------------------------------------------------
_LIC_CC0 = "CC0-1.0"
_LIC_MIT = "MIT"
_LIC_APACHE = "Apache-2.0"
_LIC_CC_BY_4 = "CC-BY-4.0"
_LIC_CC_BY_SA_4 = "CC-BY-SA-4.0"
_LIC_PUBLIC_DOMAIN = "Public-Domain"

# ---------------------------------------------------------------------------
# Collection constants (top-level groupings in the reference library)
# ---------------------------------------------------------------------------
_COL_AGENTIC_AI = "agentic-ai"
_COL_DATA_AND_ANALYSIS = "data-and-analysis"
_COL_ENGINEERING = "engineering"
_COL_SECURITY = "security"
_COL_OPERATING_MODELS = "operating-models"
_COL_PRODUCT_AND_DESIGN = "product-and-design"
_COL_LEADERSHIP_AND_CULTURE = "leadership-and-culture"
_COL_ECONOMICS_AND_STRATEGY = "economics-and-strategy"
_COL_PERSONAL_EFFECTIVENESS = "personal-effectiveness"
_COL_HEALTH_AND_FITNESS = "health-and-fitness"
_COL_PHILOSOPHY = "philosophy"
_COL_FAMILY_AND_EDUCATION = "family-and-education"
_COL_INDUSTRY_STANDARDS = "industry-standards"
_COL_FOUNDATIONS = "foundations"

# ---------------------------------------------------------------------------
# Common source URLs (repeated across multiple SourceDef entries)
# ---------------------------------------------------------------------------
_URL_GUTENBERG = "https://www.gutenberg.org"


@dataclass(frozen=True)
class SourceDef:
    """Definition of a single source within the reference library."""

    name: str
    """Human-readable source name (e.g. 'OpenAI Cookbook')."""

    collection: str
    """Top-level collection directory (e.g. 'agentic-ai')."""

    dir_name: str
    """Subdirectory name under collection (e.g. 'openai-cookbook')."""

    licence: str
    """SPDX licence identifier (e.g. 'MIT', 'CC0-1.0', 'Apache-2.0')."""

    licence_tier: int
    """1=CC0/PD/Unlicense, 2=MIT/Apache/MPL, 3=CC-BY, 4=CC-BY-SA, 5+=NC/ND/proprietary."""

    source_url: str
    """Canonical source URL (typically GitHub)."""

    exclude_patterns: tuple[str, ...] = ()
    """Path substring patterns to exclude beyond global boilerplate."""

    format: str = "markdown"
    """Primary format: 'markdown', 'json', 'yaml', 'text', 'pdf'."""


def _key(collection: str, dir_name: str) -> str:
    return f"{collection}/{dir_name}"


# ---------------------------------------------------------------------------
# Registry — every source in the reference library
# ---------------------------------------------------------------------------

_SOURCES: list[SourceDef] = [
    # ── agentic-ai ─────────────────────────────────────────────────────────
    SourceDef(
        name="OpenAI Cookbook",
        collection=_COL_AGENTIC_AI,
        dir_name="openai-cookbook",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/openai/openai-cookbook",
    ),
    SourceDef(
        name="DAIR.AI Prompt Engineering Guide",
        collection=_COL_AGENTIC_AI,
        dir_name="dair-ai-prompts",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/dair-ai/Prompt-Engineering-Guide",
    ),
    SourceDef(
        name="Panaversity Learn Agentic AI",
        collection=_COL_AGENTIC_AI,
        dir_name="panaversity-agentic",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/panaversity/learn-agentic-ai",
        exclude_patterns=("translations/",),
    ),
    SourceDef(
        name="Microsoft Generative AI for Beginners",
        collection=_COL_AGENTIC_AI,
        dir_name="ms-gen-ai-beginners",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/microsoft/generative-ai-for-beginners",
        exclude_patterns=("translations/",),
    ),
    SourceDef(
        name="Microsoft Prompts for Education",
        collection=_COL_AGENTIC_AI,
        dir_name="ms-prompts-edu",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/microsoft/prompts-for-edu",
    ),
    SourceDef(
        name="Awesome AI System Prompts",
        collection=_COL_AGENTIC_AI,
        dir_name="awesome-ai-system-prompts",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/dontriskit/awesome-ai-system-prompts",
    ),
    SourceDef(
        name="Microsoft AutoGen",
        collection=_COL_AGENTIC_AI,
        dir_name="autogen-docs",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/microsoft/autogen",
        exclude_patterns=(
            "test/",
            "samples/",
            "notebook/",
        ),
    ),
    SourceDef(
        name="EleutherAI LM Evaluation Harness",
        collection=_COL_AGENTIC_AI,
        dir_name="eleutherai-lm-eval",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/EleutherAI/lm-evaluation-harness",
    ),
    SourceDef(
        name="Stanford HELM",
        collection=_COL_AGENTIC_AI,
        dir_name="stanford-helm",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/stanford-crfm/helm",
    ),
    # ── data-and-analysis ──────────────────────────────────────────────────
    SourceDef(
        name="dbt Core Documentation",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="dbt-docs",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/dbt-labs/docs.getdbt.com",
        exclude_patterns=("blog/",),
    ),
    SourceDef(
        name="PostHog Documentation",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="posthog-docs",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/PostHog/posthog.com",
        exclude_patterns=(
            "contents/customers/",
            "contents/blog/",
            "contents/founders/",
            "contents/newsletter/",
            "contents/spotlight/",
            "contents/media/",
        ),
    ),
    SourceDef(
        name="MLOps Guide",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="mlops-guide",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/MLOps-Guide/MLOps-Guide",
    ),
    SourceDef(
        name="The Turing Way",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="turing-way",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/the-turing-way/the-turing-way",
        exclude_patterns=("translation",),
    ),
    SourceDef(
        name="Causal Inference for the Brave and True",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="causal-inference-handbook",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/matheusfacure/python-causality-handbook",
    ),
    SourceDef(
        name="GrowthBook",
        collection=_COL_DATA_AND_ANALYSIS,
        dir_name="growthbook-docs",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/growthbook/growthbook",
        exclude_patterns=("packages/",),
    ),
    # ── engineering ────────────────────────────────────────────────────────
    SourceDef(
        name="Architecture Decision Records (JPH)",
        collection=_COL_ENGINEERING,
        dir_name="adr-examples",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/joelparkerhenderson/architecture-decision-record",
    ),
    SourceDef(
        name="Markdown ADR (MADR)",
        collection=_COL_ENGINEERING,
        dir_name="madr",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/adr/madr",
    ),
    SourceDef(
        name="Open Source SOC Documentation",
        collection=_COL_ENGINEERING,
        dir_name="soc-docs",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/madirish/ossocdocs",
    ),
    SourceDef(
        name="18F Guides",
        collection=_COL_ENGINEERING,
        dir_name="18f-guides",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/18F/guides",
    ),
    SourceDef(
        name="Twelve-Factor App",
        collection=_COL_ENGINEERING,
        dir_name="12factor",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/heroku/12factor",
    ),
    SourceDef(
        name="Microsoft REST API Guidelines",
        collection=_COL_ENGINEERING,
        dir_name="microsoft-api-guidelines",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/microsoft/api-guidelines",
    ),
    SourceDef(
        name="Microsoft Code-With Engineering Playbook",
        collection=_COL_ENGINEERING,
        dir_name="microsoft-code-with-playbook",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/microsoft/code-with-engineering-playbook",
    ),
    SourceDef(
        name="Google Engineering Practices",
        collection=_COL_ENGINEERING,
        dir_name="google-eng-practices",
        licence="CC-BY-3.0",
        licence_tier=3,
        source_url="https://github.com/google/eng-practices",
    ),
    SourceDef(
        name="GDS Way (UK Gov Digital Service)",
        collection=_COL_ENGINEERING,
        dir_name="gds-way",
        licence="OGL-3.0",
        licence_tier=3,
        source_url="https://github.com/alphagov/gds-way",
    ),
    SourceDef(
        name="OpenTelemetry Documentation",
        collection=_COL_ENGINEERING,
        dir_name="opentelemetry-docs",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/open-telemetry/opentelemetry.io",
        exclude_patterns=(
            "static/",
            "layouts/",
            "i18n/",
        ),
    ),
    SourceDef(
        name="arc42 Architecture Template",
        collection=_COL_ENGINEERING,
        dir_name="arc42-template",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/arc42/arc42-template",
    ),
    SourceDef(
        name="Dropbox Engineering Career Framework",
        collection=_COL_ENGINEERING,
        dir_name="dropbox-career-framework",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/dropbox/dbx-career-framework",
    ),
    SourceDef(
        name="Engineering Ladders (jorgef)",
        collection=_COL_ENGINEERING,
        dir_name="engineering-ladders",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/jorgef/engineeringladders",
    ),
    # ── security ───────────────────────────────────────────────────────────
    SourceDef(
        name="OWASP Cheat Sheet Series",
        collection=_COL_SECURITY,
        dir_name="owasp-cheat-sheets",
        licence=_LIC_CC_BY_SA_4,
        licence_tier=4,
        source_url="https://github.com/OWASP/CheatSheetSeries",
    ),
    SourceDef(
        name="SLSA Specification",
        collection=_COL_SECURITY,
        dir_name="slsa-spec",
        licence="Community-Spec-1.0",
        licence_tier=4,
        source_url="https://github.com/slsa-framework/slsa",
    ),
    SourceDef(
        name="CycloneDX SBOM Specification",
        collection=_COL_SECURITY,
        dir_name="cyclonedx-spec",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/CycloneDX/specification",
    ),
    SourceDef(
        name="Openlane GRC Platform",
        collection=_COL_SECURITY,
        dir_name="openlane-grc",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/theopenlane/core",
    ),
    # ── operating-models ───────────────────────────────────────────────────
    SourceDef(
        name="CNCF TAG App Delivery (Platform Engineering)",
        collection=_COL_OPERATING_MODELS,
        dir_name="cncf-platform-model",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/cncf/tag-app-delivery",
    ),
    SourceDef(
        name="Ways of Working (JPH)",
        collection=_COL_OPERATING_MODELS,
        dir_name="jph-ways-of-working",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/joelparkerhenderson/ways-of-working",
    ),
    # ── product-and-design ─────────────────────────────────────────────────
    SourceDef(
        name="Gong Product Practices",
        collection=_COL_PRODUCT_AND_DESIGN,
        dir_name="gong-practices",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/gong-io/product-practices",
    ),
    SourceDef(
        name="USDS Digital Services Playbook",
        collection=_COL_PRODUCT_AND_DESIGN,
        dir_name="usds-playbook",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/usds/playbook",
    ),
    SourceDef(
        name="Awesome Retrospectives",
        collection=_COL_PRODUCT_AND_DESIGN,
        dir_name="awesome-retrospectives",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/josephearl/awesome-retrospectives",
    ),
    # ── leadership-and-culture ─────────────────────────────────────────────
    SourceDef(
        name="Awesome Open Company",
        collection=_COL_LEADERSHIP_AND_CULTURE,
        dir_name="awesome-open-company",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/opencompany/awesome-open-company",
    ),
    SourceDef(
        name="Awesome Developing (JPH)",
        collection=_COL_LEADERSHIP_AND_CULTURE,
        dir_name="jph-awesome-developing",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/joelparkerhenderson/awesome-developing",
    ),
    SourceDef(
        name="Mozilla Open Leadership Framework",
        collection=_COL_LEADERSHIP_AND_CULTURE,
        dir_name="mozilla-open-leadership",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/mozilla/open-leadership-framework",
    ),
    SourceDef(
        name="Ontario Service Design Playbook",
        collection=_COL_LEADERSHIP_AND_CULTURE,
        dir_name="ontario-service-design",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/ongov/Service-Design-Playbook",
    ),
    # ── economics-and-strategy ─────────────────────────────────────────────
    SourceDef(
        name="Business Model Canvas (JPH)",
        collection=_COL_ECONOMICS_AND_STRATEGY,
        dir_name="jph-business-model-canvas",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/joelparkerhenderson/business-model-canvas",
    ),
    SourceDef(
        name="Startup Business Guide (SixArm/JPH)",
        collection=_COL_ECONOMICS_AND_STRATEGY,
        dir_name="jph-startup-guide",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/SixArm/startup-business-guide",
    ),
    SourceDef(
        name="Meta Robyn (Marketing Mix Modelling)",
        collection=_COL_ECONOMICS_AND_STRATEGY,
        dir_name="meta-robyn-mmm",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/facebookexperimental/Robyn",
    ),
    SourceDef(
        name="Google Meridian MMM",
        collection=_COL_ECONOMICS_AND_STRATEGY,
        dir_name="google-meridian-mmm",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/google/meridian",
    ),
    SourceDef(
        name="PyMC-Marketing (Bayesian MMM)",
        collection=_COL_ECONOMICS_AND_STRATEGY,
        dir_name="pymc-marketing",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/pymc-labs/pymc-marketing",
    ),
    # ── personal-effectiveness ─────────────────────────────────────────────
    SourceDef(
        name="Objectives and Key Results (JPH)",
        collection=_COL_PERSONAL_EFFECTIVENESS,
        dir_name="jph-okrs",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/joelparkerhenderson/objectives-and-key-results",
    ),
    SourceDef(
        name="Open Spaced Repetition (FSRS)",
        collection=_COL_PERSONAL_EFFECTIVENESS,
        dir_name="open-spaced-repetition",
        licence=_LIC_MIT,
        licence_tier=2,
        source_url="https://github.com/open-spaced-repetition/fsrs4anki",
    ),
    SourceDef(
        name="Mindful Programming",
        collection=_COL_PERSONAL_EFFECTIVENESS,
        dir_name="mindful-programming",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/code-in-flow/mindful-programming",
    ),
    # ── health-and-fitness ─────────────────────────────────────────────────
    SourceDef(
        name="Free Exercise Database",
        collection=_COL_HEALTH_AND_FITNESS,
        dir_name="free-exercise-db",
        licence="Unlicense",
        licence_tier=1,
        source_url="https://github.com/yuhonas/free-exercise-db",
        format="json",
    ),
    SourceDef(
        name="Awesome Quantified Self",
        collection=_COL_HEALTH_AND_FITNESS,
        dir_name="awesome-quantified-self",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/woop/awesome-quantified-self",
    ),
    SourceDef(
        name="Awesome Healthcare",
        collection=_COL_HEALTH_AND_FITNESS,
        dir_name="awesome-healthcare",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/kakoni/awesome-healthcare",
    ),
    SourceDef(
        name="Awesome Mental Health",
        collection=_COL_HEALTH_AND_FITNESS,
        dir_name="awesome-mental-health",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/dreamingechoes/awesome-mental-health",
    ),
    SourceDef(
        name="Circadiaware (Circadian Health)",
        collection=_COL_HEALTH_AND_FITNESS,
        dir_name="circadiaware",
        licence=_LIC_CC_BY_SA_4,
        licence_tier=4,
        source_url="https://github.com/Circadiaware/VLiDACMel-entrainment-therapy-non24",
    ),
    # ── philosophy ─────────────────────────────────────────────────────────
    SourceDef(
        name="Tao Te Ching (Standard Ebooks)",
        collection=_COL_PHILOSOPHY,
        dir_name="standard-ebooks-tao-te-ching",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/standardebooks/laozi_tao-te-ching_james-legge",
    ),
    SourceDef(
        name="Art of War (Standard Ebooks)",
        collection=_COL_PHILOSOPHY,
        dir_name="standard-ebooks-art-of-war",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/standardebooks/sun-tzu_the-art-of-war_lionel-giles",
    ),
    SourceDef(
        name="SuttaCentral (Buddhist Suttas)",
        collection=_COL_PHILOSOPHY,
        dir_name="suttacentral",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/suttacentral/sc-data",
    ),
    SourceDef(
        name="Bhagavad Gita (Structured Data)",
        collection=_COL_PHILOSOPHY,
        dir_name="bhagavad-gita-data",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/vedicscriptures/bhagavad-gita-data",
        format="json",
    ),
    SourceDef(
        name="Eastern Classical Texts (Gutenberg)",
        collection=_COL_PHILOSOPHY,
        dir_name="classical-eastern",
        licence=_LIC_PUBLIC_DOMAIN,
        licence_tier=1,
        source_url=_URL_GUTENBERG,
        format="text",
    ),
    SourceDef(
        name="Western Classical Texts (Gutenberg)",
        collection=_COL_PHILOSOPHY,
        dir_name="classical-western",
        licence=_LIC_PUBLIC_DOMAIN,
        licence_tier=1,
        source_url=_URL_GUTENBERG,
        format="text",
    ),
    SourceDef(
        name="Indian Philosophy (Gutenberg)",
        collection=_COL_PHILOSOPHY,
        dir_name="indian-philosophy",
        licence=_LIC_PUBLIC_DOMAIN,
        licence_tier=1,
        source_url=_URL_GUTENBERG,
        format="text",
    ),
    SourceDef(
        name="Martial Arts Philosophy (Gutenberg)",
        collection=_COL_PHILOSOPHY,
        dir_name="martial-arts-philosophy",
        licence=_LIC_PUBLIC_DOMAIN,
        licence_tier=1,
        source_url=_URL_GUTENBERG,
        format="text",
    ),
    # ── family-and-education ───────────────────────────────────────────────
    SourceDef(
        name="Awesome Parenting",
        collection=_COL_FAMILY_AND_EDUCATION,
        dir_name="awesome-parenting",
        licence=_LIC_CC0,
        licence_tier=1,
        source_url="https://github.com/daugaard/awesome-parenting",
    ),
    # Note: Montessori Method and Dewey are at collection root, not in subdirs.
    # They are registered as dir_name="" with the collection as the key.
    # ── industry-standards ─────────────────────────────────────────────────
    SourceDef(
        name="BIAN Semantic APIs",
        collection=_COL_INDUSTRY_STANDARDS,
        dir_name="bian-apis",
        licence=_LIC_APACHE,
        licence_tier=2,
        source_url="https://github.com/bian-official/public",
        format="yaml",
    ),
    SourceDef(
        name="MOSIP Documentation",
        collection=_COL_INDUSTRY_STANDARDS,
        dir_name="mosip-docs",
        licence="MPL-2.0",
        licence_tier=2,
        source_url="https://github.com/mosip/documentation",
    ),
    # ── foundations ─────────────────────────────────────────────────────────
    SourceDef(
        name="Open Logic Project",
        collection=_COL_FOUNDATIONS,
        dir_name="open-logic-project",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/OpenLogicProject/OpenLogic",
    ),
    SourceDef(
        name="Neuromatch Computational Neuroscience",
        collection=_COL_FOUNDATIONS,
        dir_name="neuromatch-compneuro",
        licence=_LIC_CC_BY_4,
        licence_tier=3,
        source_url="https://github.com/NeuromatchAcademy/course-content",
    ),
]

# Build lookup dict
SOURCES: dict[str, SourceDef] = {_key(s.collection, s.dir_name): s for s in _SOURCES}


def get_source(collection: str, dir_name: str) -> SourceDef | None:
    """Look up a source definition by collection and directory name."""
    return SOURCES.get(_key(collection, dir_name))


def get_allowed_sources(max_tier: int = 3) -> list[SourceDef]:
    """Return all sources at or below the given licence tier."""
    return [s for s in _SOURCES if s.licence_tier <= max_tier]


def get_excluded_sources(max_tier: int = 3) -> list[SourceDef]:
    """Return sources above the given licence tier (excluded from normalisation)."""
    return [s for s in _SOURCES if s.licence_tier > max_tier]


def all_collections() -> list[str]:
    """Return sorted list of unique collection names."""
    return sorted({s.collection for s in _SOURCES})
