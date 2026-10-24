"""Contributor attribution must reach distributions and installed payloads."""
from pathlib import Path
import runpy
try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTION = ("CONTRIBUTORS.md", "CHANGELOG.md")


def test_distribution_declares_both_attribution_documents():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    shipped = metadata["tool"]["setuptools"]["data-files"]["share/hermes-nerve"]
    assert set(ATTRIBUTION) <= set(shipped)


def test_isolated_runtime_install_preserves_actual_credit_bytes(tmp_path):
    helper = runpy.run_path(str(ROOT / "scripts/install_runtime_payload.py"))
    destination = tmp_path / "isolated-runtime"
    helper["install"](ROOT, destination)
    for name in ATTRIBUTION:
        delivered = destination / name
        assert delivered.is_file(), f"Installed runtime omitted attribution: {name}"
        assert delivered.read_bytes() == (ROOT / name).read_bytes()
    assert "@phuongvm" in (destination / "CONTRIBUTORS.md").read_text()
    assert "@rharmanca" in (destination / "CONTRIBUTORS.md").read_text()
