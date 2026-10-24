"""Dependency-free, declarative registry; importing it starts no services."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    name: str
    description: str
    dependencies: tuple[str, ...] = ()


_SPECS = (
    ModuleSpec('reflex', 'Reflex core', 'Cheap typed review through Jev/Laya/OpenJev.'),
    ModuleSpec('nervous', 'Nervous system', 'Supervise decisions and turn admission.'),
    ModuleSpec('work_supervision', 'Work supervision', 'DoD, evidence and completion checks for workers.'),
    ModuleSpec('token_trajectory', 'Token trajectory', 'Watch worker budget, repetition and likely completion.', ('work_supervision',)),
    ModuleSpec('action_gate', 'Action gate', 'Review individual tool actions before execution.'),
    ModuleSpec('context_governor', 'Context governor', 'Curate/recover context and protect important state.'),
    ModuleSpec('remote_workers', 'Remote workers', 'Delegate and control Hermes workers on other hosts.'),
    ModuleSpec('shared_context', 'Shared Context', 'Share bounded handoffs across Hermes agents/surfaces.'),
    ModuleSpec('assistant_loops', 'Assistant loops', 'Persist goals, dependencies, triggers and follow-through.'),
    ModuleSpec('assistant_audit', 'Assistant audit', 'Reflex-check active goals during normal Hermes turns.', ('assistant_loops', 'reflex')),
    ModuleSpec('shadow_testing', 'Shadow testing', 'Compare Reflex backends for development/calibration.', ('reflex',)),
    ModuleSpec('receipts', 'Receipts', 'Keep bounded evidence for supervisory decisions.'),
    ModuleSpec('local_learning', 'Local learning', 'Learn from bounded local supervisory outcomes.', ('nervous',)),
    ModuleSpec('verified_experience', 'Verified experience', 'Persist evidence-backed outcomes and bounded successor-run memories.'),
    ModuleSpec('selective_context', 'Selective context', 'Plan selective context admission without mandatory provider calls.'),
    ModuleSpec('selective_context_memory', 'Selective memory', 'Select which verified memory candidates to admit.', ('selective_context',)),
    ModuleSpec('selective_context_tools', 'Selective tools', 'Prioritize relevant tool schemas for active tasks.', ('selective_context',)),
    ModuleSpec('selective_context_skills', 'Selective skills', 'Prioritize skill descriptions for task context.', ('selective_context',)),
)
MODULES = MappingProxyType({spec.id: spec for spec in _SPECS})
MODULE_REGISTRY = MODULES