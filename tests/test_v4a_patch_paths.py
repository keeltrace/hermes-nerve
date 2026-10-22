import unittest

from hermes_nerve.progress_integrity import mutation_is_relevant, mutation_paths

V4A = """*** Begin Patch
*** Update File: /work/sandbox/app/calc.py
@@
-    return a-b
+    return a+b
*** Add File: docs/NOTES.md
+hello
*** End Patch
"""


class V4APatchPathTests(unittest.TestCase):
    def test_v4a_paths_extracted(self):
        self.assertEqual(mutation_paths("patch", {"mode": "patch", "patch": V4A}), ("/work/sandbox/app/calc.py", "docs/NOTES.md"))

    def test_v4a_patch_counts_as_relevant_mutation(self):
        self.assertTrue(mutation_is_relevant("patch", {"mode": "patch", "patch": V4A}, ("app/calc.py", "app/test_calc.py")))

    def test_v4a_unrelated_file_not_relevant(self):
        self.assertFalse(mutation_is_relevant("patch", {"mode": "patch", "patch": V4A}, ("src/other.py",)))

    def test_unified_diff_still_supported(self):
        diff = "--- a/app/calc.py\n+++ b/app/calc.py\n@@\n-x\n+y\n"
        self.assertIn("app/calc.py", mutation_paths("patch", {"patch": diff}))


if __name__ == "__main__":
    unittest.main()
