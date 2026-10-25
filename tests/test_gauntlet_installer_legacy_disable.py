"""Legacy disabling is a real ordering/failure gate, never an ignored hint.

These are synthetic CLI transaction tests, not native activation qualification.
"""
import pytest
from test_gauntlet_installer_payload import staged_installer as staged_installer


@pytest.mark.parametrize('injection', ['disable_failure', 'require_present_manifest'])
def test_legacy_disable_precedes_backup_and_failure_stops_install(staged_installer, injection):
    source, profile, dest, activation, run = staged_installer
    legacy = dest.parent / 'hermes-jev'
    legacy.mkdir()
    (legacy / 'old-marker').write_text('old legacy bytes')
    fake = source.parent / 'fakebin' / 'hermes'
    order = source.parent / 'disable-order.txt'
    script = fake.read_text()
    intercept = '''if [ "$4" = disable ]; then
    if [ -f "$HOME/.hermes/profiles/$2/plugins/hermes-jev/old-marker" ]; then
        printf 'present' > "$SOURCE_CHECKOUT/../disable-order.txt"
    else
        printf 'missing' > "$SOURCE_CHECKOUT/../disable-order.txt"
        exit 48
    fi
'''
    intercept += '    exit 47\n' if injection == 'disable_failure' else '    exit 0\n'
    intercept += 'fi\n'
    assert script.startswith('#!/bin/sh\n')
    fake.write_text(script.replace('#!/bin/sh\n', '#!/bin/sh\n' + intercept, 1))
    before = {name: (profile / name).read_bytes() for name in ('config.yaml', 'users.json')}
    result = run()
    if injection == 'disable_failure':
        assert result.returncode == 47, result.stdout + result.stderr
        assert not activation.exists()
        assert (dest / 'old-marker').read_text() == 'old plugin bytes'
        assert (legacy / 'old-marker').read_text() == 'old legacy bytes'
        assert not list(dest.parent.glob('*.pre-*'))
        assert {name: (profile / name).read_bytes() for name in before} == before
        assert 'Installed Nerve' not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert activation.exists()
    assert order.read_text() == 'present', 'legacy manifest moved before CLI name resolution'
