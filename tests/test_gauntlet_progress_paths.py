"""Requested source-file lists must not manufacture impossible path targets."""
from hermes_nerve.progress_integrity import requested_paths, mutation_is_relevant


def test_slash_joined_source_filenames_are_independent_targets():
    paths = requested_paths("Fix hooks.py/block_review.py and verify tests")
    assert paths == ("hooks.py", "block_review.py")
    assert mutation_is_relevant("patch", {"path": "/project/hermes_nerve/work/hooks.py"}, paths)
    assert mutation_is_relevant("patch", {"path": "/project/hermes_nerve/work/block_review.py"}, paths)
    assert not mutation_is_relevant("patch", {"path": "/unrelated/client.py"}, paths)


def test_true_directories_and_dotted_directories_stay_intact():
    assert requested_paths("Fix hermes_nerve/work/hooks.py") == ("hermes_nerve/work/hooks.py",)
    assert requested_paths("Fix v0.4.0/hermes_nerve/hooks.py") == ("v0.4.0/hermes_nerve/hooks.py",)
