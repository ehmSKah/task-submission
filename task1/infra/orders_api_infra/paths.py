"""Locations shared by the CDK app, the build scripts and the tests."""

from pathlib import Path

PROJECT_DIR = Path(__file__).parents[2]
BUILD_DIR = PROJECT_DIR / "build"
BUNDLE_DIR = BUILD_DIR / "lambda"
SPEC_PATH = PROJECT_DIR / "openapi" / "openapi.yaml"
CONFIG_DIR = PROJECT_DIR / "infra" / "config"
