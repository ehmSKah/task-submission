"""Build the Lambda code bundle for one environment (ADRs 0009 and 0010).

Dependencies are installed as manylinux wheels for the target architecture with uv, so building
needs no Docker. One bundle serves both functions; the OpenAPI document ships inside it.

Usage: python -m orders_api_infra.bundle --env local
"""

import argparse
import shutil
import subprocess
from pathlib import Path

from orders_api_infra.config import load_config
from orders_api_infra.paths import BUILD_DIR, BUNDLE_DIR, PROJECT_DIR, SPEC_PATH

PYTHON_VERSION = "3.13"


def build(env: str) -> Path:
    architecture = load_config(env).resolved_architecture()
    shutil.rmtree(BUNDLE_DIR, ignore_errors=True)
    BUNDLE_DIR.mkdir(parents=True)
    requirements = BUILD_DIR / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-default-groups",
            "--no-emit-project",
            "--no-hashes",
            "--quiet",
            "--output-file",
            str(requirements),
        ],
        cwd=PROJECT_DIR,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--target",
            str(BUNDLE_DIR),
            "--python-platform",
            architecture.manylinux_platform,
            "--python-version",
            PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "--requirements",
            str(requirements),
        ],
        cwd=PROJECT_DIR,
        check=True,
    )
    shutil.copytree(
        PROJECT_DIR / "src" / "orders_api",
        BUNDLE_DIR / "orders_api",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy(SPEC_PATH, BUNDLE_DIR / "orders_api" / "contract")
    print(f"Built {BUNDLE_DIR.relative_to(PROJECT_DIR)} for {architecture.value}")
    return BUNDLE_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    build(parser.parse_args().env)


if __name__ == "__main__":
    main()
