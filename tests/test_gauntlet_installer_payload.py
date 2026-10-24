"""Actual installer safety in a tiny synthetic checkout; activation is a fake CLI."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def staged_installer(tmp_path):
    source = tmp_path / "source"
    scripts = source / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install_dev17_profile.sh", scripts)
    helper = ROOT / "scripts/install_runtime_payload.py"
    if helper.exists():
        shutil.copy2(helper, scripts)
    shutil.copy2(ROOT / "scripts/install_profile_transaction.py", scripts)
    files = {
        "plugin.yaml": "name: nerve\nversion: 0.4.11\n",
        "__init__.py": "# synthetic plugin entry\n",
        "pyproject.toml": (ROOT / "pyproject.toml").read_text(),
        "README.md": "Synthetic fixture\n",
        "LICENSE": "Synthetic license\n",
        "CONTRIBUTORS.md": "Synthetic credit: @phuongvm and @rharmanca\n",
        "CHANGELOG.md": "Synthetic release notes\n",
        "hermes_nerve/__init__.py": "",
        "hermes_nerve/provenance.py": "VERSION = '0.4.11'\n",
        "hermes_jev/__init__.py": "",
        "hermes_nerve/web/index.html": "<p>fixture</p>",
        "hermes_nerve/web/app.js": "// fixture",
        "hermes_nerve/web/app.css": "/*fixture*/",
        "hermes_nerve/university/content/sample.json": "{}",
        "hermes_nerve/observatory/assets/factory.js": "// fixture",
        "hermes_nerve/observatory/assets/factory.css": "/*fixture*/",
        "hermes_nerve/research/note.md": "Fixture research",
        ".env": "SYNTHETIC_PRIVATE_VALUE=not-a-real-secret\n",
        ".git": "gitdir: synthetic-private-checkout\n",
        "reports/private.txt": "synthetic private report\n",
        "tests/private_fixture.py": "# not a plugin module\n",
        "hermes_nerve/private.env": "synthetic non-runtime package file\n",
    }
    for relative, content in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    home = tmp_path / "home's staged owner"
    profile = home / ".hermes/profiles/qualification"
    dest = profile / "plugins/hermes-nerve"
    dest.mkdir(parents=True)
    (dest / "old-marker").write_text("old plugin bytes")
    (profile / "config.yaml").write_text("synthetic: preserved\n")
    (profile / "users.json").write_text('{"fixture":"preserved"}\n')
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake = fakebin / "hermes"
    fake.write_text('''#!/bin/sh
if [ "$3" = plugins ] && [ "$4" = validate ]; then
    printf '%s\\n' "$5" > "$VALIDATION_MARKER"
    [ ! -e "$5/.env" ] || exit 42
    [ ! -f "$VALIDATION_FAILURE_FLAG" ] || exit 1
    cp "$5/hermes_nerve/provenance.py" "$VALIDATED_PROVENANCE"
    if [ -f "$VALIDATION_CHANGE_SOURCE" ]; then
        printf "VERSION = '9.9.9'\\n" > "$SOURCE_CHECKOUT/hermes_nerve/provenance.py"
    fi
    exit 0
fi
printf '%s\\n' "$*" > "$ACTIVATION_MARKER"
''')
    fake.chmod(0o700)
    marker = tmp_path / "activation.txt"
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(profile), PATH=str(fakebin)+os.pathsep+os.environ["PATH"], ACTIVATION_MARKER=str(marker),
               VALIDATION_MARKER=str(tmp_path / "validation.txt"),
               VALIDATION_FAILURE_FLAG=str(tmp_path / "reject-validation"),
               VALIDATION_CHANGE_SOURCE=str(tmp_path / "change-source-during-validation"),
               VALIDATED_PROVENANCE=str(tmp_path / "validated-provenance.py"), SOURCE_CHECKOUT=str(source))
    def run():
        return subprocess.run(["bash", str(scripts / "install_dev17_profile.sh"), "qualification"], env=env, capture_output=True, text=True, timeout=5)
    return source, profile, dest, marker, run


@pytest.mark.parametrize("private_path", [".env", ".git", "reports/private.txt", "tests/private_fixture.py", "hermes_nerve/private.env"])
def test_installer_never_ships_checkout_private_files(staged_installer, private_path):
    source, profile, dest, marker, run = staged_installer
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (dest / private_path).exists(), f"Non-runtime checkout file was installed: {private_path}"


@pytest.mark.parametrize("broken_source", ["missing_manifest", "symlink_module", "invalid_python"])
def test_bad_source_is_rejected_before_existing_deployment_changes(staged_installer, broken_source):
    source, profile, dest, marker, run = staged_installer
    if broken_source == "missing_manifest":
        (source / "plugin.yaml").unlink()
    elif broken_source == "symlink_module":
        outside = source.parent / "synthetic-private.py"
        outside.write_text("# synthetic non-runtime input\n")
        (source / "hermes_nerve/escaped.py").symlink_to(outside)
    else:
        (source / "hermes_nerve/broken.py").write_text("def invalid syntax(\n")
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert (dest / "old-marker").read_text() == "old plugin bytes"
    assert not marker.exists(), "Activation must not run on invalid source"
    assert not list(dest.parent.rglob("hermes-nerve.pre-dev17-*"))
    assert (profile / "config.yaml").read_text() == "synthetic: preserved\n"
    assert (profile / "users.json").read_text() == '{"fixture":"preserved"}\n'


def test_selected_runtime_assets_backup_and_profile_data_are_preserved(staged_installer):
    source, profile, dest, marker, run = staged_installer
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    for package in ("hermes_nerve", "hermes_jev"):
        for original in (source / package).rglob("*"):
            if original.is_file() and original.suffix != ".env":
                assert (dest / original.relative_to(source)).read_bytes() == original.read_bytes()
    for name in ("plugin.yaml", "__init__.py", "pyproject.toml", "README.md", "LICENSE"):
        assert (dest / name).read_bytes() == (source / name).read_bytes()
    backups = list(dest.parent.rglob("hermes-nerve.pre-dev17-*"))
    assert len(backups) == 1
    assert (backups[0] / "old-marker").read_text() == "old plugin bytes"
    assert (profile / "config.yaml").read_text() == "synthetic: preserved\n"
    assert (profile / "users.json").read_text() == '{"fixture":"preserved"}\n'
    assert marker.read_text().strip() == "-p qualification plugins enable nerve"
    assert "Installed Nerve 0.4.11" in result.stdout


def test_runtime_import_failure_never_reports_success_or_activates(staged_installer):
    source, profile, dest, marker, run = staged_installer
    (source / "hermes_nerve/provenance.py").write_text("raise RuntimeError('synthetic import failure')\n")
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Installed Nerve" not in result.stdout
    assert not marker.exists()
    backups = list(dest.parent.rglob("hermes-nerve.pre-dev17-*"))
    assert not backups, "Version preflight must fail before moving the deployment"
    assert (dest / "old-marker").read_text() == "old plugin bytes"
    assert (profile / "config.yaml").read_text() == "synthetic: preserved\n"
    assert (profile / "users.json").read_text() == '{"fixture":"preserved"}\n'


def test_failed_native_admission_preserves_both_installations(staged_installer):
    source, profile, dest, marker, run = staged_installer
    legacy = dest.parent / "hermes-jev"
    legacy.mkdir()
    (legacy / "legacy-marker").write_text("legacy plugin bytes")
    (marker.parent / "reject-validation").touch()
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert (dest / "old-marker").read_text() == "old plugin bytes"
    assert (legacy / "legacy-marker").read_text() == "legacy plugin bytes"
    assert not marker.exists()
    assert not list(dest.parent.rglob("*.pre-*-*"))
    assert (profile / "config.yaml").read_text() == "synthetic: preserved\n"
    assert (profile / "users.json").read_text() == '{"fixture":"preserved"}\n'
    assert "Installed Nerve" not in result.stdout
    staged = Path((marker.parent / "validation.txt").read_text().strip())
    assert staged != source
    assert not staged.exists(), "Rejected temporary payload must be cleaned up"


def test_deploys_exact_validated_payload_despite_checkout_change(staged_installer):
    source, profile, dest, marker, run = staged_installer
    (marker.parent / "change-source-during-validation").touch()
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "9.9.9" in (source / "hermes_nerve/provenance.py").read_text()
    assert (dest / "hermes_nerve/provenance.py").read_bytes() == (marker.parent / "validated-provenance.py").read_bytes()
    assert "Installed Nerve 0.4.11" in result.stdout


def test_cli_payload_excludes_hidden_and_backup_runtime_candidates(staged_installer):
    source, profile, dest, marker, run = staged_installer
    package = source / 'hermes_nerve'
    private = ('.env.py', '.git/private.py', '.venv/private.py',
               'nested/.cache/private.py', 'scratch.bak.py',
               'web/app.bak.js', 'web/.env.js', 'research/notes.bak.md')
    for name in private:
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'PRIVATE_FIXTURE = True\n')
    public = {'nested/allowed.py': b'public_fixture = True\n',
              'web/app.js': b'// public runtime fixture\n',
              'research/safety.md': b'Public runtime documentation\n'}
    for name, data in public.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    result = run()
    assert result.returncode == 0, result.stderr
    shipped = {str(path.relative_to(dest / 'hermes_nerve'))
               for path in (dest / 'hermes_nerve').rglob('*') if path.is_file()}
    assert not set(private) & shipped, 'Checkout-private runtime candidates were installed'
    for name, data in public.items():
        assert (dest / 'hermes_nerve' / name).read_bytes() == data
    assert marker.exists(), 'The fixture must reach the native validation/activation path'


@pytest.mark.parametrize("backup_kind", ["current", "legacy"])
@pytest.mark.parametrize("collision_kind", ["directory", "dangling_symlink"])
@pytest.mark.parametrize("location", ["legacy_sibling", "archive"])
def test_backup_collision_is_rejected_before_either_deployment_moves(staged_installer, backup_kind, collision_kind, location):
    source, profile, dest, marker, run = staged_installer
    legacy = dest.parent / "hermes-jev"
    legacy.mkdir()
    (legacy / "legacy-marker").write_text("legacy plugin bytes")
    fake_date = marker.parent / "fakebin/date"
    fake_date.write_text("#!/bin/sh\nprintf '%s\\n' '20261022T000000'\n")
    fake_date.chmod(0o700)
    basename = "hermes-nerve.pre-dev17" if backup_kind == "current" else "hermes-jev.pre-nerve"
    storage = dest.parent if location == "legacy_sibling" else dest.parent / "__nerve_backups__"
    storage.mkdir(exist_ok=True)
    backup = storage / (basename + "-20261022T000000")
    if collision_kind == "directory":
        backup.mkdir()
        (backup / "prior-backup-marker").write_text("prior backup bytes")
    else:
        backup.symlink_to(marker.parent / "nonexistent-target", target_is_directory=True)
    result = run()
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Refusing existing backup path" in result.stderr
    assert (dest / "old-marker").read_text() == "old plugin bytes"
    assert (legacy / "legacy-marker").read_text() == "legacy plugin bytes"
    assert not marker.exists(), "Neither legacy disable nor activation may run"
    assert "Installed Nerve" not in result.stdout
    if collision_kind == "directory":
        assert sorted(p.name for p in backup.iterdir()) == ["prior-backup-marker"]
        assert (backup / "prior-backup-marker").read_text() == "prior backup bytes"
    else:
        assert backup.is_symlink()
        assert backup.readlink() == marker.parent / "nonexistent-target"
    assert (profile / "config.yaml").read_text() == "synthetic: preserved\n"
    assert (profile / "users.json").read_text() == '{"fixture":"preserved"}\n'


def test_runtime_asset_allowlist_matches_declared_package_metadata():
    import runpy
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    helper = runpy.run_path(str(ROOT / "scripts/install_runtime_payload.py"))
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = metadata["tool"]["setuptools"]["package-data"]
    expected = {f"{package}/{pattern}" for package, patterns in declared.items() for pattern in patterns}
    assert set(helper["ASSET_GLOBS"]) == expected
