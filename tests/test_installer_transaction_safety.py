"""Parent adversarial qualification; native CLI is stubbed, filesystem is real."""
import errno
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def setup_case(tmp_path, *, current=True, legacy=True, config=True):
    spec = importlib.util.spec_from_file_location('parent_tx', ROOT / 'scripts/install_profile_transaction.py')
    assert spec is not None and spec.loader is not None
    tx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tx)
    profile = tmp_path / 'profiles/qualification'
    profile.mkdir(parents=True)
    stage = tmp_path / 'stage'
    stage.mkdir()
    for name in ('plugin.yaml', '__init__.py', 'pyproject.toml', 'README.md', 'LICENSE',
                 'CONTRIBUTORS.md', 'CHANGELOG.md', 'hermes_nerve/__init__.py',
                 'hermes_nerve/provenance.py', 'hermes_jev/__init__.py'):
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# synthetic runtime\n' if target.suffix == '.py' else 'fixture\n')
    dest, old = profile / 'plugins/hermes-nerve', profile / 'plugins/hermes-jev'
    for present, folder in ((current, dest), (legacy, old)):
        if present:
            folder.mkdir(parents=True)
            folder.chmod(0o750)
            (folder / 'old').write_bytes(b'old\x00deployment')
            (folder / 'old').chmod(0o640)
    if config:
        (profile / 'config.yaml').write_bytes(b'old config\n')
        (profile / 'config.yaml').chmod(0o600)
    (profile / 'users.json').write_bytes(b'untouched users\n')
    (profile / 'users.json').chmod(0o640)
    data = profile / 'data/nerve/state'
    data.parent.mkdir(parents=True)
    data.write_bytes(b'persistent state\x00')
    data.chmod(0o600)
    protected = (dest, old, profile / 'config.yaml', profile / 'users.json', data.parent)
    before = {p: tx.fingerprint(p) for p in protected}
    argv = ['install_profile_transaction.py', str(ROOT), str(stage), str(profile),
            str(dest.parent / '__nerve_backups__/hermes-nerve.pre-dev17-fixed'),
            str(dest.parent / '__nerve_backups__/hermes-jev.pre-nerve-fixed'), 'qualification']
    calls = []

    def command(*args):
        args = [str(a) for a in args]
        calls.append(args)
        if args[0] == 'hermes':
            operation = args[-2]
            (profile / 'config.yaml').write_text(operation + ' changed config\n')
            if operation == 'enable':
                raise subprocess.CalledProcessError(47, args)
        elif args[0] == 'mv':
            os.rename(args[-2], args[-1])
        else:
            assert args[0] == 'python3'
            subprocess.run([sys.executable, '-B', *args[1:]], check=True, capture_output=True)

    return SimpleNamespace(tx=tx, profile=profile, stage=stage, dest=dest, legacy=old,
                           argv=argv, calls=calls, command=command, before=before)


def execute(case):
    with patch.object(sys, 'argv', case.argv), patch.object(case.tx, 'command', side_effect=case.command):
        return case.tx.main()


def assert_restored(case):
    assert {p: case.tx.fingerprint(p) for p in case.before} == case.before


def test_config_restore_survives_profile_on_distinct_filesystem(tmp_path):
    case = setup_case(tmp_path)
    real_replace = os.replace

    def mounted_profile_replace(source, destination):
        # Simulate profile mounted on a distinct filesystem from its parent.
        if Path(source).is_relative_to(case.profile) != Path(destination).is_relative_to(case.profile):
            raise OSError(errno.EXDEV, 'injected cross-filesystem config restore')
        return real_replace(source, destination)

    with patch.object(case.tx.os, 'replace', side_effect=mounted_profile_replace):
        code = execute(case)
    assert code == 47
    assert_restored(case)


def test_incomplete_recovery_preserves_failed_config_until_all_preimages_validate(tmp_path):
    case = setup_case(tmp_path, config=False)
    ordinary = case.command

    def corrupt_preimage(*args):
        if args[0] == 'hermes' and args[-2] == 'enable':
            (case.profile / 'config.yaml').write_bytes(b'failed candidate diagnostic config\n')
            (Path(case.argv[4]) / 'old').unlink()
            raise subprocess.CalledProcessError(47, args)
        return ordinary(*args)

    case.command = corrupt_preimage
    assert execute(case) == 70
    assert (case.profile / 'config.yaml').read_bytes() == b'failed candidate diagnostic config\n'
    assert list(case.profile.glob('nerve-recovery-*'))
    assert (Path(case.argv[5]) / 'old').read_bytes() == b'old\x00deployment'


@pytest.mark.parametrize('current', (False, True))
@pytest.mark.parametrize('legacy', (False, True))
@pytest.mark.parametrize('config', (False, True))
def test_activation_failure_restores_all_existing_and_absent_topologies(tmp_path, current, legacy, config):
    case = setup_case(tmp_path, current=current, legacy=legacy, config=config)
    assert execute(case) == 47
    assert_restored(case)


@pytest.mark.parametrize('activation_failure', (False, True))
def test_native_discovery_does_not_find_archived_or_failed_plugins(tmp_path, activation_failure):
    discovery = pytest.importorskip('hermes_cli.plugins_discovery')
    case = setup_case(tmp_path)
    manifest = 'name: nerve\nversion: 0.4.11\ndescription: synthetic archive-discovery fixture\n'
    for folder in (case.stage, case.dest):
        (folder / 'plugin.yaml').write_text(manifest)
        (folder / '__init__.py').write_text('raise AssertionError("manifest discovery must never import this")\n')
    ordinary = case.command

    def native(*args):
        if not activation_failure and args[0] == 'hermes' and args[-2] == 'enable':
            return
        return ordinary(*args)

    case.command = native
    assert execute(case) == (47 if activation_failure else 0)
    manifests = discovery.scan_directory(case.dest.parent, 'user')
    assert len([item for item in manifests if item.name == 'nerve']) == 1


@pytest.mark.parametrize('after_creation', (False, True))
def test_destination_mkdir_failure_restores_preimages(tmp_path, after_creation):
    case = setup_case(tmp_path)
    real_mkdir = Path.mkdir

    def interrupted_mkdir(path, *args, **kwargs):
        if path == case.dest and not after_creation:
            raise OSError(errno.EIO, 'injected failure before mkdir')
        result = real_mkdir(path, *args, **kwargs)
        if path == case.dest:
            raise OSError(errno.EIO, 'injected interruption after mkdir')
        return result

    with patch.object(Path, 'mkdir', interrupted_mkdir):
        code = execute(case)
    assert code != 70 and code != 0
    assert_restored(case)
