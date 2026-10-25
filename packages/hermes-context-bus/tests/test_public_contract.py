from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"

class PublicContractTests(unittest.TestCase):
    def test_nerve_compatibility_contract(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("name: hermes-context-bus", text)
        self.assertIn('version: "0.2.0"', text)
        self.assertIn("  - shared_context_health", text)

    def test_no_machine_specific_paths_or_private_markers(self):
        banned = [
            "/home/" + "j/",
            "/srv/" + "mega-mcp/",
            "Documents/" + "LargeProjects",
            "free" + "brain",
            "run-" + "mcp.sh",
            "docs/" + "KEELTRACE.md",
        ]
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part.startswith(".") for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for marker in banned:
                self.assertNotIn(marker, text, str(path))

if __name__ == "__main__":
    unittest.main()
