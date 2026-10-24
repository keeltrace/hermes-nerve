"""Provider boundary regressions discovered by the release gauntlet (offline)."""
import pytest

from hermes_nerve.client import JevClient, JevError


@pytest.mark.parametrize("response", [
    ("OK", b'{}'),
    (None, b'{}'),
    ({}, b'{}'),
    ("200.5", b'{}'),
    (200, 123),
    (200, b'[]'),
    (200, b'null'),
    (200, b'\xff'),
])
def test_malformed_transport_response_is_a_provider_error(response):
    client = JevClient(api_key="offline-test-not-a-secret", transport=lambda *args: response)
    with pytest.raises(JevError):
        client.system_one(state="test", questions={"decision": {"type": "boolean"}})


@pytest.mark.parametrize("status", [200.5, 200.0])
def test_fractional_or_float_status_cannot_be_truncated_into_success(status):
    client = JevClient(
        api_key="offline-test-not-a-secret",
        transport=lambda *args: (status, b'{"answers": {"decision": {"choice": "ALLOW"}}}'),
    )
    with pytest.raises(JevError, match="invalid HTTP status"):
        client.system_one(state="test", questions={"decision": {"type": "boolean"}})
