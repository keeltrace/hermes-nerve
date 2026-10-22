import unittest
from unittest.mock import patch

from hermes_nerve import client


def secrets(**vals):
    return lambda name: vals.get(name, "")


class ProviderDefaultTests(unittest.TestCase):
    def setUp(self):
        client.configure()  # clear configured provider

    def make(self, **keys):
        with patch.object(client, "_resolve_secret", side_effect=secrets(**keys)), patch.dict("os.environ", {}, clear=False) as env:
            env.pop("HERMES_NERVE_PROVIDER", None)
            return client.JevClient(transport=lambda *a: (200, b"{}", {}))

    def test_typesafe_only_key_selects_typesafe(self):
        self.assertEqual(self.make(TYPESAFE_API_KEY="t").provider_kind, "typesafe")

    def test_openrouter_only_key_selects_openrouter(self):
        self.assertEqual(self.make(OPENROUTER_API_KEY="o").provider_kind, "openrouter")

    def test_both_keys_keep_openrouter_default(self):
        self.assertEqual(self.make(OPENROUTER_API_KEY="o", TYPESAFE_API_KEY="t").provider_kind, "openrouter")

    def test_explicit_setting_wins(self):
        client.configure(provider="openrouter")
        try:
            self.assertEqual(self.make(OPENROUTER_API_KEY="o", TYPESAFE_API_KEY="t").provider_kind, "openrouter")
            client.configure(provider="typesafe")
            self.assertEqual(self.make(OPENROUTER_API_KEY="o", TYPESAFE_API_KEY="t").provider_kind, "typesafe")
        finally:
            client.configure()

    def test_openrouter_402_hints_typesafe(self):
        keys = dict(OPENROUTER_API_KEY="o", TYPESAFE_API_KEY="t")
        with patch.object(client, "_resolve_secret", side_effect=secrets(**keys)):
            c = client.JevClient(transport=lambda *a: (402, b'{"error":"Insufficient credits"}', {}))
            with self.assertRaises(client.JevError) as cm:
                c.system_one(state={"x": 1}, questions={"q": {"type": "choice", "instructions": "i", "criteria": {"A": None, "B": None}}})
        self.assertIn("jev_provider: typesafe", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
