"""Native catalog reads must retain the reviewed credential-store policy."""
import pytest

from hermes_nerve import catalog, gate, risk


@pytest.fixture(autouse=True)
def isolated_owner(tmp_path, monkeypatch):
    owner = tmp_path / 'owner'
    owner.mkdir()
    monkeypatch.setenv('HOME', str(owner))
    monkeypatch.setattr(risk, '_HOME', owner)
    monkeypatch.chdir(tmp_path)


def shaped_path(shape, path):
    if shape == 'path':
        return {'path': path}
    if shape == 'file_path':
        return {'file_path': path}
    if shape == 'paths':
        return {'paths': [path]}
    if shape == 'nested':
        return {'nested': {'path': path}}
    return {'uri': 'file://' + path}


@pytest.mark.parametrize('filename', ['hosts.yml', 'secring.gpg'])
@pytest.mark.parametrize('shape', ['path', 'file_path', 'paths', 'nested', 'uri'])
def test_credential_store_outside_owner_home_cannot_bypass(shape, filename):
    # Synthetic paths are classified only; nothing reads credential contents.
    target = '/synthetic-other-owner/.config/' + filename
    args = shaped_path(shape, target)
    assert catalog.sensitive_path_args(args) == [target]
    assert risk.assess_tool_call('read_file', args).category == risk.CONSEQUENTIAL
    assessed = catalog.classify_tool('read_file', args)
    assert assessed is not None and assessed.category == risk.CONSEQUENTIAL
    assert not assessed.auto_allow
    assert gate.bypass_reason('read_file', args) is None


@pytest.mark.parametrize('shape', ['path', 'file_path', 'paths', 'nested', 'uri'])
def test_ordinary_read_shapes_retain_catalog_bypass(shape):
    args = shaped_path(shape, '/synthetic-project/README.md')
    assessed = catalog.classify_tool('read_file', args)
    assert assessed is not None and assessed.category == risk.SAFE
    assert assessed.auto_allow
    assert gate.bypass_reason('read_file', args) == 'catalog-read-only'
