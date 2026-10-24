"""RED/GREEN regressions for the Nerve release verifier (scripts/verify_release.py).

These tests pin the release-identity contract that the verifier enforces:

  * the package version is resolved from pyproject's *dynamic* declaration
    (``[tool.setuptools.dynamic] version = {attr = "hermes_nerve._version.__version__"}``),
    not from a hardcoded literal;
  * plugin.yaml, provenance.VERSION and packaging/hermes-catalog/nerve.yaml all agree
    with the resolved package version;
  * a deliberate version mismatch is detected and rejected (RED);
  * the verifier's environment mutations are restored (no env leakage).
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_VERSION = "0.4.14"


def _load_verifier():
    """Import scripts/verify_release.py as a module (it is a script, not a package member)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_release_under_test", ROOT / "scripts" / "verify_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_verifier_subprocess() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_release.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=25,
    )


class TestVersionMismatchIsRejected:
    """RED: a deliberate version mismatch must fail the verifier."""

    def test_pyproject_dynamic_version_mismatch_rejected(self, monkeypatch):
        verifier = _load_verifier()
        # Point the dynamic attr at a module whose __version__ is stale.
        import hermes_nerve._version as version_module

        monkeypatch.setattr(version_module, "__version__", "0.3.2.2")
        py = {
            "project": {"dynamic": ["version"]},
            "tool": {"setuptools": {"dynamic": {"version": {"attr": "hermes_nerve._version.__version__"}}}},
        }
        with pytest.raises(AssertionError):
            verifier.check_versions(py, EXPECTED_VERSION)

    def test_catalog_version_mismatch_rejected(self, monkeypatch):
        verifier = _load_verifier()
        py = {"project": {"version": EXPECTED_VERSION}}
        monkeypatch.setattr(verifier, "plugin_version", lambda: EXPECTED_VERSION)
        monkeypatch.setattr(verifier, "catalog_version", lambda: "0.3.2.2")
        import hermes_nerve.provenance as provenance

        monkeypatch.setattr(provenance, "VERSION", EXPECTED_VERSION)
        with pytest.raises(AssertionError):
            verifier.check_versions(py, EXPECTED_VERSION)

    def test_plugin_version_mismatch_rejected(self, monkeypatch):
        verifier = _load_verifier()
        py = {"project": {"version": EXPECTED_VERSION}}
        monkeypatch.setattr(verifier, "plugin_version", lambda: "0.3.2.2")
        with pytest.raises(AssertionError):
            verifier.check_versions(py, EXPECTED_VERSION)

    def test_end_to_end_version_mismatch_fails(self):
        """End-to-end RED: stale _version.py causes verifier subprocess exit=1."""
        tmp = Path(tempfile.mkdtemp())
        try:
            dst = tmp / "repo"
            shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
            # Deliberately mismatch the resolved package version.
            (dst / "hermes_nerve" / "_version.py").write_text('__version__ = "0.3.2.2"\n')
            r = subprocess.run(
                [sys.executable, str(dst / "scripts" / "verify_release.py")],
                cwd=str(dst),
                capture_output=True,
                text=True,
                env={**dict(os.environ), "PYTHONPATH": str(dst) + os.pathsep + dict(os.environ).get("PYTHONPATH", "")},
                timeout=25,
            )
            assert r.returncode != 0, f"Expected exit 1 for version mismatch, got {r.returncode}"
            assert "version" in r.stderr.lower() or "version" in r.stdout.lower(), (
                f"Expected version mismatch message; stderr={r.stderr[:200]}; stdout={r.stdout[:200]}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_end_to_end_version_mismatch_in_catalog_rejected(self):
        """RED: stale catalog.yaml causes verifier subprocess exit=1."""
        import shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            dst = tmp / "repo"
            shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
            # Mutate catalog.yaml version
            cat = dst / "packaging" / "hermes-catalog" / "nerve.yaml"
            original = cat.read_text()
            try:
                cat.write_text(original.replace("0.4.14", "0.3.2.2"))
                r = subprocess.run(
                    [sys.executable, str(dst / "scripts" / "verify_release.py")],
                    cwd=str(dst),
                    capture_output=True,
                    text=True,
                    env={**dict(os.environ), "PYTHONPATH": str(dst) + os.pathsep + dict(os.environ).get("PYTHONPATH", "")},
                    timeout=25,
                )
                assert r.returncode != 0, f"Expected exit 1 for catalog version mismatch, got {r.returncode}"
            finally:
                cat.write_text(original)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestVersionSourcesAgree:
    """GREEN: the real release identity sources agree at 0.4.14."""

    def test_verify_release_passes_end_to_end(self):
        result = _run_verifier_subprocess()
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"version={EXPECTED_VERSION}" in result.stdout
        assert "catalog_version=0.4.14" in result.stdout

    def test_resolve_package_version_honors_dynamic_declaration(self):
        verifier = _load_verifier()
        py = tomllib.loads((ROOT / "pyproject.toml").read_text())
        assert "version" in py["project"].get("dynamic", []), "pyproject must declare a dynamic version"
        resolved = verifier.resolve_package_version(py)
        assert resolved == EXPECTED_VERSION

    def test_catalog_yaml_version_matches(self):
        text = (ROOT / "packaging" / "hermes-catalog" / "nerve.yaml").read_text()
        m = re.search(r'^version:\s*["\']([^"\']+)', text, re.M)
        assert m, "catalog.yaml is missing a version"
        assert m.group(1) == EXPECTED_VERSION

    def test_plugin_yaml_version_matches(self):
        text = (ROOT / "plugin.yaml").read_text()
        m = re.search(r'^version:\s*["\']([^"\']+)', text, re.M)
        assert m, "plugin.yaml is missing a version"
        assert m.group(1) == EXPECTED_VERSION

    def test_provenance_version_matches(self):
        from hermes_nerve.provenance import VERSION

        assert VERSION == EXPECTED_VERSION

    def test_no_core_dependencies_and_laya_pinned(self):
        py = tomllib.loads((ROOT / "pyproject.toml").read_text())
        assert py["project"]["dependencies"] == []
        assert py["project"]["optional-dependencies"]["laya"] == ["laya==0.3.29"]


class TestEnvironmentRestored:
    """The verifier must not leak the environment it mutates."""

    def test_kanban_and_verify_env_restored(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "pre-test-value")
        monkeypatch.setenv("HERMES_NERVE_OFFLINE_VERIFY", "1")
        verifier = _load_verifier()
        monkeypatch.setattr(verifier, "main", verifier.main)
        # main() runs the full registration path including the headless-worker block.
        assert verifier.main() == 0
        assert os.environ.get("HERMES_KANBAN_TASK") == "pre-test-value"
        assert os.environ.get("HERMES_NERVE_OFFLINE_VERIFY") == "1"

    def test_environment_restored_after_registration_failure(self, monkeypatch):
        verifier = _load_verifier()
        keys = ("HERMES_HOME", "HERMES_KANBAN_TASK", "HERMES_KANBAN_TASK_ID",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
                "HERMES_NERVE_OFFLINE_VERIFY")
        for key in keys:
            monkeypatch.setenv(key, "owner-sentinel-" + key)
        before = {key: os.environ.get(key) for key in keys}

        def fail_registration(*args, **kwargs):
            raise RuntimeError("injected registration failure")

        monkeypatch.setattr(verifier.Ctx, "register_tool", fail_registration)
        with pytest.raises(RuntimeError, match="injected registration failure"):
            verifier.main()
        assert {key: os.environ.get(key) for key in keys} == before

    def test_hermes_home_restored(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/tmp/verify-home-sentinel")
        verifier = _load_verifier()
        assert verifier.main() == 0
        assert os.environ.get("HERMES_HOME") == "/tmp/verify-home-sentinel"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
