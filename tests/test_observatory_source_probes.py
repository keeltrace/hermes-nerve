"""Provenance and failure isolation for read-only control-room service probes."""
import unittest
from unittest.mock import patch
from hermes_nerve.observatory import source_probes


class SourceProbeTests(unittest.TestCase):
    def test_all_fixed_sources(self):
        with patch.object(source_probes, "_fetch", return_value={"status": "ok"}), \
             patch.object(source_probes, "_mega_mcp_state", return_value="ok"):
            report = source_probes.collect()
        self.assertEqual(report["schema"], source_probes.SCHEMA)
        self.assertEqual([x["id"] for x in report["sources"]],
                         ["commandcenter", "inference-gateway", "paperclip", "mega-mcp"])
        self.assertEqual(report["sources"][0]["state"], "unknown")  # requires ready
        self.assertTrue(all("observed_at" in x for x in report["sources"]))

    def test_no_cached_green_after_failures(self):
        with patch.object(source_probes, "_fetch", side_effect=TimeoutError), \
             patch.object(source_probes, "_mega_mcp_state", side_effect=OSError):
            report = source_probes.collect()
        self.assertTrue(all(x["state"] == "unknown" for x in report["sources"]))

    def test_accepted_exact_health_labels(self):
        statuses = {"commandcenter": "ready", "inference-gateway": "ok", "paperclip": "ok"}
        def fake(url):
            name = "commandcenter" if "4173" in url else "inference-gateway" if "20128" in url else "paperclip"
            return {"status": statuses[name]}
        with patch.object(source_probes, "_fetch", side_effect=fake), \
             patch.object(source_probes, "_mega_mcp_state", return_value="ok"):
            report = source_probes.collect()
        self.assertTrue(all(x["state"] == "ok" for x in report["sources"]))

    def test_loopback_only_and_no_user_supplied_paths(self):
        for _, _, url, _ in source_probes._SOURCES:
            self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertEqual(source_probes.collect.__code__.co_argcount, 0)


if __name__ == "__main__":
    unittest.main()
