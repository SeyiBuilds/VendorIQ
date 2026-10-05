"""Shared pytest fixtures. Adds backend/agents and backend/data/schemas to
the import path so tests can import agent modules without a package install
step — matches how the agents import each other today."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # project root
sys.path.insert(0, str(ROOT / "backend" / "agents"))
sys.path.insert(0, str(ROOT / "backend" / "data" / "schemas"))

SAMPLE_MATCHES_DIR = ROOT / "backend" / "data" / "sample_matches"
