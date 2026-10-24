"""Browser geometry and neighborhood color contracts evaluated with real JS."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from hermes_nerve.observatory import ui


def function_source(name):
    match = re.search(r"^  function " + re.escape(name) + r"\(.*?\n  \}\n", ui.APP_JS, re.M | re.S)
    if not match:
        raise AssertionError(name + " must remain testable in shipping JS")
    return match.group(0)


@unittest.skipUnless(shutil.which("node"), "Node required for shipping JS parity")
class AtlasJsTests(unittest.TestCase):
    def node_eval(self, source):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "atlas.js"
            path.write_text(source, encoding="utf-8")
            p = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_frame_units_match_python_1000_width_normalization(self):
        js = (function_source("localBounds") + function_source("frameOf") +
              "const good=frameOf({bounds:[20,30,4000,2800]});\n" +
              "const bad=frameOf({bounds:[0,0,-3,10]});\n" +
              "console.log(JSON.stringify({good,bad,local:localBounds([20,30,4000,2800],good)}));")
        # frameOf checks validity via localBounds, nothing may be invented.
        result = self.node_eval(js)
        self.assertEqual(result["good"], {"ox": 20, "oy": 30, "units": 4})
        self.assertEqual(result["local"], [0, 0, 1000, 700])
        self.assertIsNone(result["bad"])

    def test_task_palette_is_stable_and_based_on_real_task_identity(self):
        js = (function_source("atlasTaskColor") +
              "console.log(JSON.stringify([" +
              "atlasTaskColor('t_0fd7240e'),atlasTaskColor('t_0fd7240e')," +
              "atlasTaskColor('t_d9cf962b')]));")
        output = self.node_eval(js)
        self.assertEqual(output[0], output[1])
        self.assertRegex(output[0], r"^#[0-9a-f]{6}$")
        self.assertNotEqual(output[0], output[2])


if __name__ == "__main__":
    unittest.main()
