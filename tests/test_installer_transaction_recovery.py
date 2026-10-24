"""Focused installer transaction RED/GREEN regressions (synthetic CLI).

No native Hermes executable is invoked; a stubbed hermes CLI drives disable,
enable, and validate. The canonical repo, installed core/profile, native PM,
and any download are untouched by this test. Dev workspace only.
"""

import os
import shlex
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _mk_selective_source(t, source):
    required = [
        "plugin.yaml", "__init__.py", "pyproject.toml", "README.md", "LICENSE",
        "CONTRIBUTORS.md", "CHANGELOG.md", "hermes_nerve/__init__.py",
        "hermes_nerve/provenance.py", "hermes_jev/__init__.py",
    ]
    for name in required:
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("__init__.py"):
            p.write_text("# synthetic\n")
        else:
            p.write_text("synthetic\n")
    for rel, content in {
        "hermes_nerve/provenance.py": "VERSION = '0.4.11'\n",
        "hermes_nerve/web/index.html": "<p>x</p>",
        "hermes_nerve/web/app.js": "// x",
        "hermes_nerve/web/app.css": "/*x*/",
        "hermes_nerve/research/note.md": "x",
    }.items():
        p = source / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _mk_fakebin(t, profile, calls_path, case):
    bin_dir = t / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = t / "calls"
    calls.write_text("")
    date_stub = bin_dir / "date"
    date_stub.write_text("#!/bin/sh\nprintf 'fixed\\n'\n")
    date_stub.chmod(0o700)
    hermes = bin_dir / "hermes"
    hermes.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "P = pathlib.Path\n"
        "argv = sys.argv[1:]\n"
        "case = os.environ.get('CASE', '')\n"
        "op = ''\n"
        "if 'plugins' in argv and 'validate' in argv: op = 'validate'\n"
        "elif 'plugins' in argv and 'disable' in argv: op = 'disable'\n"
        "elif 'plugins' in argv and 'enable' in argv: op = 'enable'\n"
        "with open(os.environ['CALLS'], 'a') as f:\n"
        "    f.write(op + '\\n')\n"
        "if op == 'validate':\n"
        "    sys.exit(0)\n"
        "if op == 'disable':\n"
        "    if case == 'legacy_resolution' and not (P(os.environ['HERMES_HOME']) / 'plugins/hermes-jev').is_dir():\n"
        "        (P(os.environ['HERMES_HOME']) / 'legacy-unresolved').write_text('legacy disappeared')\n"
        "        sys.exit(49)\n"
        "    (P(os.environ['HERMES_HOME']) / 'config.yaml').write_text('legacy disabled\\n')\n"
        "    sys.exit(48 if case == 'disable' else 0)\n"
        "if op == 'enable':\n"
        "    if case in ('corrupt_config', 'missing_config'):\n"
        "        s = next(P(os.environ['HERMES_HOME']).glob('nerve-recovery-*/config.before'))\n"
        "        if case == 'corrupt_config': s.write_bytes(b'corrupted')\n"
        "        else: s.unlink()\n"
        "    if case in ('corrupt_backup', 'missing_backup'):\n"
        "        s = next(P(os.environ['HERMES_HOME']).glob('plugins/__nerve_backups__/hermes-nerve.pre-*/old'))\n"
        "        if case == 'corrupt_backup': s.write_bytes(b'corrupted')\n"
        "        else: s.unlink()\n"
        "    (P(os.environ['HERMES_HOME']) / 'config.yaml').write_text('new enabled\\n')\n"
        "    sys.exit(47 if case in ('activation','corrupt_config','missing_config','corrupt_backup','missing_backup','recovery_move') else 0)\n"
        "sys.exit(99)\n"
    )
    hermes.chmod(0o700)
    mv = bin_dir / "mv"
    mv.write_text(
        "#!/bin/sh\n"
        "args=$(printf '%s\\n' \"$@\" | grep -v '^-T$' | grep -v -- --)\n"
        "src=$(echo \"$args\" | head -1)\n"
        "if [ \"$CASE\" = 'recovery_move' ] && echo \"$src\" | grep -q '.pre-'; then\n"
        "    exit 51\n"
        "fi\n"
        "if [ \"$CASE\" = 'move_current' ] && [ \"$src\" = \"$HERMES_HOME/plugins/hermes-nerve\" ]; then\n"
        "    exit 46\n"
        "fi\n"
        "if [ \"$CASE\" = 'move_legacy' ] && [ \"$src\" = \"$HERMES_HOME/plugins/hermes-jev\" ]; then\n"
        "    exit 46\n"
        "fi\n"
        "exec /bin/mv \"$@\"\n"
    )
    mv.chmod(0o700)
    python3 = bin_dir / "python3"
    python3.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "P = pathlib.Path\n"
        "case = os.environ.get('CASE', '')\n"
        "args = sys.argv[1:]\n"
        "dest = P(os.environ['HERMES_HOME']) / 'plugins/hermes-nerve'\n"
        "if args and args[0].endswith('install_runtime_payload.py') and len(args) == 3 and str(args[-1]) == str(dest) and case == 'partial_copy':\n"
        "    (dest / 'partial').write_bytes(b'partial')\n"
        "    sys.stderr.write('injected storage failure after first destination write\\n')\n"
        "    sys.exit(45)\n"
        "os.execv(sys.executable, [sys.executable] + args)\n"
    )
    python3.chmod(0o700)
    return bin_dir


def _setup_profile(t):
    home = t / "home"
    profile = home / ".hermes/profiles/qualification"
    dest = profile / "plugins/hermes-nerve"
    legacy = dest.parent / "hermes-jev"
    dest.mkdir(parents=True)
    (dest / "old").write_bytes(b"old\x00bytes")
    legacy.mkdir(parents=True)
    (legacy / "old").write_bytes(b"old\x00bytes")
    (profile / "config.yaml").write_bytes(b"old config\n")
    (profile / "users.json").write_bytes(b"old data\n")
    data = profile / "data/nerve"
    data.mkdir(parents=True)
    (data / "state").write_bytes(b"old state")
    return home, profile, dest, legacy


@pytest.mark.parametrize("case", [
    "success", "move_legacy", "move_current", "partial_copy", "activation",
    "disable", "legacy_resolution", "corrupt_config", "missing_config",
    "corrupt_backup", "missing_backup", "recovery_move", "collision", "symlink",
])
def test_installer_transaction(case, tmp_path):
    with tempfile.TemporaryDirectory(prefix="tx-", dir=tmp_path) as td:
        t = Path(td)
        source = t / "source"
        source.mkdir()
        _mk_selective_source(t, source)
        (source / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/install_dev17_profile.sh", source / "scripts" / "install_dev17_profile.sh")
        shutil.copy2(ROOT / "scripts/install_runtime_payload.py", source / "scripts" / "install_runtime_payload.py")
        shutil.copy2(ROOT / "scripts/install_profile_transaction.py", source / "scripts" / "install_profile_transaction.py")
        home, profile, dest, legacy = _setup_profile(t)
        bin_dir = _mk_fakebin(t, str(profile), str(t / "calls"), case)
        (t / "tmp").mkdir()
        env = {
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
            "HOME": str(home),
            "HERMES_HOME": str(profile),
            "TMPDIR": str(t / "tmp"),
            "CASE": case,
            "CALLS": str(t / "calls"),
            "LC_ALL": "C",
        }
        if case == "symlink":
            (profile / "config.yaml").unlink()
            (t / "redirect").write_bytes(b"old config\n")
            (profile / "config.yaml").symlink_to(t / "redirect")
        if case == "collision":
            (dest.parent / "hermes-nerve.pre-dev17-fixed").symlink_to(t / "absent")
        cp = subprocess.run(
            ["/bin/bash", str(source / "scripts" / "install_dev17_profile.sh"), "qualification"],
            env=env, text=True, capture_output=True, timeout=5,
        )
        calls = t.joinpath("calls").read_text().splitlines() if (t / "calls").exists() else []
        restored = {name: (profile / name).read_bytes() for name in ("config.yaml", "users.json")}
        ops = calls
        errors = []
        incomplete = case in ("corrupt_config", "missing_config", "corrupt_backup", "missing_backup", "recovery_move")
        failed = case in ("move_current", "move_legacy", "partial_copy", "activation", "disable", "collision", "symlink")
        if incomplete:
            if cp.returncode != 70:
                errors.append(f"recovery exit {cp.returncode} != 70")
            if "Installed Nerve" in cp.stdout:
                errors.append("false success")
            if "RECOVERY INCOMPLETE" not in cp.stderr:
                errors.append("missing recovery warning")
            if not list(profile.glob("nerve-recovery-*")):
                errors.append("recovery evidence lost")
            if case == "recovery_move" and not list(dest.parent.rglob("hermes-nerve.failed-candidate-*")):
                errors.append("failed candidate lost")
            if not list(dest.parent.rglob("hermes-jev.pre-*/old")):
                errors.append("recoverable legacy lost")
        elif failed:
            if cp.returncode == 0:
                errors.append("failure exit 0")
            if "Installed Nerve" in cp.stdout:
                errors.append("false success")
            if case == "disable" and (profile / "config.yaml").read_bytes() != b"old config\n":
                errors.append("legacy config not restored")
            if case in ("move_current", "move_legacy"):
                if not (dest / "old").exists() or not (legacy / "old").exists():
                    errors.append("deployment not restored")
                if list(dest.parent.rglob("hermes-nerve.pre-dev17-*")) or list(dest.parent.rglob("hermes-jev.pre-nerve-*")):
                    errors.append("partial backup left behind")
            if case == "partial_copy" and not (dest / "old").exists():
                errors.append("partial payload not restored")
            if case == "collision" and "Refusing existing backup path" not in cp.stderr:
                errors.append("collision not refused")
            if case == "symlink" and not (dest / "old").exists():
                errors.append("symlink config deployment moved")
        else:
            if cp.returncode != 0:
                errors.append(f"success exit {cp.returncode}")
            if not (dest / "hermes_nerve/provenance.py").exists():
                errors.append("payload absent")
            if not (profile / "config.yaml").read_text().startswith("new enabled"):
                errors.append("activation absent")
            if not list(dest.parent.rglob("hermes-nerve.pre-dev17-*")):
                errors.append("current backup absent")
            if not list(dest.parent.rglob("hermes-jev.pre-nerve-*")):
                errors.append("legacy backup absent")
            if not restored["config.yaml"].startswith(b"new enabled"):
                errors.append("config not preserved as new enabled")
        if (profile / "users.json").read_bytes() != b"old data\n":
            errors.append("users.json not preserved")
        if (profile / "data/nerve/state").read_bytes() != b"old state":
            errors.append("persistent state not preserved")
        if not incomplete and list((t / "tmp").iterdir()):
            errors.append("staging leaked")
        if ops and ops[0] != "validate":
            errors.append("admission not first")
        if case in ("success", "legacy_resolution") and ops != ["validate", "disable", "enable"]:
            errors.append("native operation sequence not preserved")
        if case == "legacy_resolution" and (profile / "legacy-unresolved").exists():
            errors.append("legacy disable ran after manifest disappeared")
        assert not errors, f"case={case}: {errors}\nstdout={cp.stdout}\nstderr={cp.stderr}"


def test_legacy_disable_restores_config_bytes(tmp_path):
    """RED: legacy disable mutates config but installer must restore exact bytes on failure."""
    with tempfile.TemporaryDirectory(prefix="red-", dir=tmp_path) as td:
        t = Path(td)
        source = t / "source"
        source.mkdir()
        _mk_selective_source(t, source)
        (source / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/install_dev17_profile.sh", source / "scripts" / "install_dev17_profile.sh")
        shutil.copy2(ROOT / "scripts/install_runtime_payload.py", source / "scripts" / "install_runtime_payload.py")
        shutil.copy2(ROOT / "scripts/install_profile_transaction.py", source / "scripts" / "install_profile_transaction.py")
        home, profile, dest, legacy = _setup_profile(t)
        # stub hermes: validate ok, disable mutates config and exits 48, enable never runs
        bin_dir = t / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "hermes"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "P = pathlib.Path\n"
            "argv = sys.argv[1:]\n"
            "if 'plugins' in argv and 'validate' in argv:\n"
            "    sys.exit(0)\n"
            "if 'plugins' in argv and 'disable' in argv:\n"
            "    (P(os.environ['HERMES_HOME']) / 'config.yaml').write_text('LEGACY DISABLED\\n')\n"
            "    sys.exit(48)\n"
            "sys.exit(99)\n"
        )
        fake.chmod(0o700)
        mv = bin_dir / "mv"
        mv.write_text("#!/bin/sh\nexec /bin/mv \"$@\"\n")
        mv.chmod(0o700)
        python3 = bin_dir / "python3"
        python3.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
        python3.chmod(0o700)
        (t / "tmp").mkdir()
        env = {
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
            "HOME": str(home),
            "HERMES_HOME": str(profile),
            "TMPDIR": str(t / "tmp"),
            "CASE": "disable",
            "LC_ALL": "C",
        }
        cp = subprocess.run(
            ["/bin/bash", str(source / "scripts" / "install_dev17_profile.sh"), "qualification"],
            env=env, text=True, capture_output=True, timeout=5,
        )
        config = profile / "config.yaml"
        assert cp.returncode != 0, "expected failure"
        assert "Installed Nerve" not in cp.stdout
        assert config.read_bytes() == b"old config\n", "config not restored"
        assert (legacy / "old").read_bytes() == b"old\x00bytes", "legacy tree not preserved"
        assert (dest / "old").read_bytes() == b"old\x00bytes", "current tree not preserved"
        assert (profile / "data/nerve/state").read_bytes() == b"old state"


def _run_tx_case(tmp_path, dest_present, legacy_present, config_present):
    t = Path(tempfile.mkdtemp(prefix="tx-", dir=tmp_path))
    source = t / "source"
    source.mkdir()
    _mk_selective_source(t, source)
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/install_dev17_profile.sh", source / "scripts" / "install_dev17_profile.sh")
    shutil.copy2(ROOT / "scripts/install_runtime_payload.py", source / "scripts" / "install_runtime_payload.py")
    shutil.copy2(ROOT / "scripts/install_profile_transaction.py", source / "scripts" / "install_profile_transaction.py")
    home = t / "home"
    profile = home / ".hermes/profiles/qualification"
    dest = profile / "plugins/hermes-nerve"
    legacy = dest.parent / "hermes-jev"
    dest.parent.mkdir(parents=True)
    if dest_present:
        dest.mkdir(parents=True)
        (dest / "old").write_bytes(b"old\x00bytes")
    if legacy_present:
        legacy.mkdir(parents=True)
        (legacy / "old").write_bytes(b"old\x00bytes")
    if config_present:
        (profile / "config.yaml").write_bytes(b"old config\n")
    (profile / "users.json").write_bytes(b"old data\n")
    bin_dir = _mk_fakebin(t, str(profile), str(t / "calls"), "success")
    (t / "tmp").mkdir()
    env = {
        "PATH": str(bin_dir) + ":/usr/bin:/bin",
        "HOME": str(home),
        "HERMES_HOME": str(profile),
        "TMPDIR": str(t / "tmp"),
        "CASE": "success",
        "CALLS": str(t / "calls"),
        "LC_ALL": "C",
    }
    cp = subprocess.run(
        ["/bin/bash", str(source / "scripts" / "install_dev17_profile.sh"), "qualification"],
        env=env, text=True, capture_output=True, timeout=5,
    )
    return cp, profile, dest, legacy


def test_legacy_only_absent_dest_recovery(tmp_path):
    cp, profile, dest, legacy = _run_tx_case(tmp_path, dest_present=False, legacy_present=True, config_present=True)
    assert cp.returncode == 0, cp.stderr
    assert (dest / "hermes_nerve/provenance.py").exists()
    assert list(dest.parent.rglob("hermes-jev.pre-*")), "legacy backup missing"
    assert (profile / "config.yaml").read_text().startswith("new enabled")


def test_current_only_legacy_absent(tmp_path):
    cp, profile, dest, legacy = _run_tx_case(tmp_path, dest_present=True, legacy_present=False, config_present=True)
    assert cp.returncode == 0, cp.stderr
    assert (dest / "hermes_nerve/provenance.py").exists()
    assert list(dest.parent.rglob("hermes-nerve.pre-dev17-*")), "current backup missing"
    assert not list(dest.parent.rglob("hermes-jev.pre-*")), "unexpected legacy backup"


def test_absent_config_recovery(tmp_path):
    cp, profile, dest, legacy = _run_tx_case(tmp_path, dest_present=True, legacy_present=True, config_present=False)
    assert cp.returncode == 0, cp.stderr
    assert (dest / "hermes_nerve/provenance.py").exists()
    assert (profile / "config.yaml").read_text().startswith("new enabled")
