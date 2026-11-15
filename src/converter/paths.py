"""Locations of the repo's data directories."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RESOURCES_DIR = REPO_ROOT / "resources"
RESEARCH_DIR = RESOURCES_DIR / "research"
SCHEMA_DIR = REPO_ROOT / "schema"
DEFAULT_CROSSWALK = RESEARCH_DIR / "crosswalk.tsv"
