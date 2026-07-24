from __future__ import annotations

import re
from pathlib import Path

import pack_companions


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_distribution_versions_match_release() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)

    assert match is not None
    assert match.group(1) == pack_companions.__version__ == "0.2.1"


def test_typed_marker_and_canonical_project_metadata_are_present() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert (ROOT / "pack_companions" / "py.typed").is_file()
    assert "https://github.com/pack-companions/pack-companions-sdk" in pyproject
    assert "https://github.com/pack-companions/pack-companions" in pyproject
    assert '"httpx>=0.27.0,<1.0"' in pyproject
    assert '"pydantic>=2.0.0,<3.0"' in pyproject
    assert '"tzdata>=2024.1"' in pyproject


def test_all_declared_public_exports_resolve() -> None:
    for name in pack_companions.__all__:
        assert hasattr(pack_companions, name), name
