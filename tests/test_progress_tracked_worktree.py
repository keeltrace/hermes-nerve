"""Progress attribution distinguishes tracked worktree source from probes."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve.progress_integrity import mutation_is_relevant, changed_paths_relevant


class TrackedWorktreeProgressTests(unittest.TestCase):
    def test_tracked_source_under_scratch_is_real_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scratch" / "candidate"
            root.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            source = root / "consumer.py"
            source.write_text("VALUE = 1\n")
            subprocess.run(["git", "-C", str(root), "add", "consumer.py"], check=True)
            self.assertTrue(mutation_is_relevant("patch", {"path": str(source)}, ()))
            self.assertTrue(changed_paths_relevant([str(source)], ()))
            probe = root / "repro.py"
            probe.write_text("pass\n")
            subprocess.run(["git", "-C", str(root), "add", "repro.py"], check=True)
            self.assertFalse(mutation_is_relevant("patch", {"path": str(probe)}, ()))
            unknown = root / "untracked.py"
            unknown.write_text("pass\n")
            self.assertFalse(changed_paths_relevant([str(unknown)], ()))
            # A supplied environment must not point attribution at another index.
            with patch.dict("os.environ", {"GIT_DIR": str(root / "nonexistent")}):
                self.assertTrue(changed_paths_relevant([str(source)], ()))
            # Missing Git evidence must fail closed for scratch paths.
            with patch("hermes_nerve.progress_integrity.subprocess.run", side_effect=OSError):
                self.assertFalse(changed_paths_relevant([str(source)], ()))


if __name__ == "__main__":
    unittest.main()
