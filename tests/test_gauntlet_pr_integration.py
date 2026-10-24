"""Cross-PR safety regressions: contribution features must preserve fail-closed checks."""
import io
import urllib.error

import pytest

from hermes_nerve import client, progress_integrity as progress


@pytest.mark.parametrize("command", [
    "uv run --offline echo pytest",
    "uv run --frozen echo pytest",
    "uvx --offline echo pytest",
    "python -c -m pytest",
    'python -c \'print("; pytest --version")\'',
    "uv run python -c 'print(\"test_fake.py\")'",
])
def test_launchers_and_quoted_code_cannot_fabricate_a_test_run(command):
    assert progress.tool_kind("terminal", {"command": command}) != "test"


def test_real_urllib_402_hint_has_no_self_reference_or_body_disclosure(monkeypatch):
    monkeypatch.setattr(client, "_resolve_secret", lambda name: "synthetic" if name == "TYPESAFE_API_KEY" else "")
    def payment_required(*args, **kwargs):
        raise urllib.error.HTTPError("https://openrouter.ai/api/alpha/decisions", 402, "Payment Required", {}, io.BytesIO(b"SYNTHETIC_PRIVATE_RESPONSE_BODY"))
    monkeypatch.setattr(client.urllib.request, "urlopen", payment_required)
    with pytest.raises(client.JevError) as exc:
        client.JevClient._urllib_transport("https://openrouter.ai/api/alpha/decisions", {}, b"{}", 1)
    assert "jev_provider: typesafe" in str(exc.value)
    assert "SYNTHETIC_PRIVATE_RESPONSE_BODY" not in str(exc.value)
