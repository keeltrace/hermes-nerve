"""Research CLI must not discover another profile's credential files."""
import sys
from pathlib import Path
import urllib.request

import pytest
from hermes_nerve.research import ask

ENV_NAMES = ('MUNA_API_BASE', 'MUNA_API_KEY', 'NOUS_API_TOKEN')


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'owner'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'current-profile'))
    for key in ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    real_exists = Path.exists
    def guard_exists(path):
        assert not str(path).endswith('/.hermes/profiles/freebrain/.env'), 'private profile probe'
        return real_exists(path)
    monkeypatch.setattr(Path, 'exists', guard_exists)
    def no_network(*args, **kwargs):
        raise AssertionError('network was not authorized')
    monkeypatch.setattr(urllib.request, 'urlopen', no_network)


def test_help_needs_no_credentials_or_private_profile_probe(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['research.ask', '--help'])
    with pytest.raises(SystemExit) as error:
        ask.main()
    assert error.value.code == 0
    assert 'question' in capsys.readouterr().out


@pytest.mark.parametrize('missing', [None, *ENV_NAMES])
def test_missing_explicit_environment_stops_before_state_or_network(monkeypatch, missing):
    if missing is not None:
        for name in ENV_NAMES:
            if name != missing:
                monkeypatch.setenv(name, 'SYNTHETIC_NOT_A_REAL_CREDENTIAL')
    monkeypatch.setattr(sys, 'argv', ['research.ask', 'synthetic question'])
    def no_ledger():
        raise AssertionError('ledger created before credential preflight')
    monkeypatch.setattr(ask, 'Ledger', no_ledger)
    with pytest.raises(RuntimeError, match='missing explicit research provider environment') as error:
        ask.main()
    assert 'SYNTHETIC_NOT_A_REAL_CREDENTIAL' not in str(error.value)
    for name in ENV_NAMES if missing is None else [missing]:
        assert name in str(error.value)


def test_explicit_environment_reaches_local_pipeline_without_discovery(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.setenv(name, 'SYNTHETIC_NOT_A_REAL_CREDENTIAL')
    monkeypatch.setattr(sys, 'argv', ['research.ask', 'synthetic question'])
    class StopBeforeEffects(Exception):
        pass
    def boundary():
        raise StopBeforeEffects()
    monkeypatch.setattr(ask, 'Ledger', boundary)
    with pytest.raises(StopBeforeEffects):
        ask.main()
