"""Progress attribution must distinguish tracked worktree source from throwaway probes."""
import subprocess
import tempfile
from pathlib import Path

from hermes_nerve.progress_integrity import mutation_is_relevant, changed_paths_relevant


def test_tracked_source_under_scratch_is_real_progress():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "scratch" / "candidate"
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        source = root / "consumer.py"
        source.write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(root), "add", "consumer.py"], check=True)
        assert mutation_is_relevant("patch", {"path": str(source)}, ())
        assert changed_paths_relevant([str(source)], ())
        probe = root / "repro.py"
        probe.write_text("pass\n")
        assert not mutation_is_relevant("patch", {"path": str(probe)}, ())
        unknown = root / "untracked.py"
        unknown.write_text("pass\n")
        assert not changed_paths_relevant([str(unknown)], ())
