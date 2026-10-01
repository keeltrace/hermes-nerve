"""Opt-in Hermes ContextEngine implementation backed by Jev context-value policy.

The engine preserves user/assistant text and valid tool-call/result structure. It
uses Jev only for semantic value estimates and deterministically anchors old tool
results when doing so is safe. Selection is opt-in through ``context.engine: jev``.
"""

from __future__ import annotations

from typing import Any

from . import context, ledger, lifecycle
from .privacy import canonical_hash

try:  # Real Hermes host.
    from agent.context_engine import ContextEngine  # type: ignore
except Exception:  # Standalone package tests/documentation.
    class ContextEngine:  # type: ignore
        last_prompt_tokens = 0
        last_completion_tokens = 0
        last_total_tokens = 0
        threshold_tokens = 0
        context_length = 0
        compression_count = 0
        threshold_percent = 0.75
        protect_first_n = 3
        protect_last_n = 6
        emit_automatic_compaction_status = True

        def update_model(self, model: str, context_length: int, **kwargs: Any) -> None:
            self.context_length = int(context_length or 0)
            self.threshold_tokens = int(self.context_length * float(self.threshold_percent))

        def on_session_reset(self) -> None:
            self.last_prompt_tokens = 0
            self.last_completion_tokens = 0
            self.last_total_tokens = 0
            self.compression_count = 0

        def get_status(self) -> dict[str, Any]:
            last = max(int(self.last_prompt_tokens or 0), 0)
            return {
                "last_prompt_tokens": last,
                "threshold_tokens": int(self.threshold_tokens or 0),
                "context_length": int(self.context_length or 0),
                "usage_percent": min(100.0, last / self.context_length * 100.0) if self.context_length else 0.0,
                "compression_count": int(self.compression_count or 0),
            }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def _tool_call_map(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "")
            raw_args = fn.get("arguments") if fn else call.get("arguments")
            args: dict[str, Any] = {}
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                import json
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        args = parsed
                except Exception:
                    args = {"raw": raw_args[:2000]}
            if call_id:
                out[call_id] = (name, args)
    return out


def _protected_indexes(messages: list[dict[str, Any]], first_n: int, last_n: int) -> set[int]:
    protected = {i for i, msg in enumerate(messages) if isinstance(msg, dict) and msg.get("role") == "system"}
    non_system = [i for i, msg in enumerate(messages) if isinstance(msg, dict) and msg.get("role") != "system"]
    protected.update(non_system[: max(0, int(first_n))])
    if last_n:
        protected.update(non_system[-max(0, int(last_n)) :])
    return protected


def _goal(messages: list[dict[str, Any]], focus_topic: str | None = None) -> str:
    if str(focus_topic or "").strip():
        return str(focus_topic).strip()
    recent = []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _content_text(msg.get("content")).strip()
            if text:
                recent.append(text)
            if len(recent) >= 3:
                break
    recent.reverse()
    return "\n\n".join(recent)[:6000] or "Preserve the evidence needed to continue the current Hermes task correctly."


def _is_jev_anchor(content: Any) -> bool:
    """Return True for anchors already emitted by this context engine."""
    return _content_text(content).lstrip().startswith("[NERVE_CONTEXT_ANCHOR ")


class NerveContextEngine(ContextEngine):
    """Conservative Jev context engine focused on tool-result evidence."""

    emit_automatic_compaction_status = False

    def __init__(
        self,
        *,
        mode: str = "shadow",
        threshold_percent: float = 0.72,
        protect_first_n: int = 3,
        protect_last_n: int = 6,
        shadow_trigger_percent: float = 0.55,
        fallback_builtin: bool = True,
    ) -> None:
        self.mode = str(mode or "shadow").strip().lower()
        if self.mode not in {"apply", "shadow"}:
            # Fail safe: only an explicit "apply" may enable destructive curation.
            self.mode = "shadow"
        self.threshold_percent = max(0.30, min(0.95, float(threshold_percent)))
        self.protect_first_n = max(0, int(protect_first_n))
        self.protect_last_n = max(1, int(protect_last_n))
        self.shadow_trigger_percent = max(0.20, min(0.95, float(shadow_trigger_percent)))
        self.fallback_builtin = bool(fallback_builtin)
        self._fallback = None
        self._model = ""
        self._route: dict[str, str] = {}
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self._session_id = ""
        self._last_shadow_signature = ""
        self._last_plan: dict[str, Any] = {}
        self._last_selection: dict[str, int] = {}
        self._curation_attempts = 0
        self._curation_fail_open_count = 0
        self._fallback_fail_open_count = 0
        self._shadow_curation_attempts = 0
        self._shadow_curation_failures = 0
        self._last_failure_stage = ""
        self._last_failure_type = ""

    @property
    def name(self) -> str:
        return "jev"

    def _build_fallback(self) -> None:
        """Best-effort built-in compressor fallback for text-heavy/no-progress sessions.

        The Jev engine owns semantic evidence curation. If there are no eligible old
        tool results to anchor, Hermes' normal compressor remains the safer last resort
        than pretending the context has been compacted. Construction is lazy so the
        standalone package never gains a hard dependency on Hermes internals.
        """
        self._fallback = None
        if not self.fallback_builtin or not self._model:
            return
        try:
            from agent.context_compressor import ContextCompressor  # type: ignore
            self._fallback = ContextCompressor(
                model=self._model,
                threshold_percent=self.threshold_percent,
                protect_first_n=self.protect_first_n,
                protect_last_n=max(self.protect_last_n, 6),
                quiet_mode=True,
                base_url=self._route.get("base_url", ""),
                api_key=self._route.get("api_key", ""),
                config_context_length=self.context_length or None,
                provider=self._route.get("provider", ""),
                api_mode=self._route.get("api_mode", ""),
            )
            if self.context_length:
                try:
                    self._fallback.update_model(
                        self._model, self.context_length,
                        base_url=self._route.get("base_url", ""),
                        api_key=self._route.get("api_key", ""),
                        provider=self._route.get("provider", ""),
                        api_mode=self._route.get("api_mode", ""),
                    )
                except TypeError:
                    self._fallback.update_model(self._model, self.context_length)
        except Exception:
            # Fail open. ContextEngine host semantics require a plugin failure to be
            # no worse than leaving the original request untouched.
            self._fallback = None

    def _fallback_compress(
        self, messages: list[dict[str, Any]], current_tokens: int | None,
        focus_topic: str | None, force: bool, memory_context: str, nerve_candidates: int = 0,
    ) -> list[dict[str, Any]]:
        """Delegate to Hermes' built-in compressor without ever breaking the turn."""
        fallback = self._fallback
        if fallback is None:
            return messages
        try:
            try:
                out = fallback.compress(
                    messages, current_tokens=current_tokens, focus_topic=focus_topic,
                    force=force, memory_context=memory_context,
                )
            except TypeError:
                try:
                    out = fallback.compress(messages, current_tokens=current_tokens, focus_topic=focus_topic)
                except TypeError:
                    out = fallback.compress(messages, current_tokens=current_tokens)
        except Exception as exc:
            self._fallback_fail_open_count += 1
            self._last_failure_stage = "fallback"
            self._last_failure_type = type(exc).__name__
            self._last_plan = {
                "contract": "context-engine/fail-open/v1",
                "stats": {
                    "fallback_failed": True,
                    "nerve_candidates": int(nerve_candidates),
                    "failure_type": self._last_failure_type,
                },
            }
            return messages
        if isinstance(out, list) and out is not messages and out != messages:
            self.compression_count += 1
            self.last_prompt_tokens = -1
            fallback_stats: dict[str, Any] = {"fallback_used": True, "nerve_candidates": int(nerve_candidates)}
            # Goal-11: preserve curation_error_type recorded before fallback so it
            # is not silently overwritten when the built-in compressor succeeds.
            prior_error_type = (self._last_plan or {}).get("stats", {}).get("curation_error_type")
            if prior_error_type:
                fallback_stats["curation_error_type"] = prior_error_type
            self._last_plan = {
                "contract": "context-engine/fallback-built-in/v1",
                "stats": fallback_stats,
            }
            return out
        return messages

    def update_from_response(self, usage: dict[str, Any]) -> None:
        def _int(*keys: str) -> int:
            for key in keys:
                try:
                    if usage.get(key) is not None:
                        return int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
            return 0

        self.last_prompt_tokens = _int("prompt_tokens", "input_tokens")
        self.last_completion_tokens = _int("completion_tokens", "output_tokens")
        self.last_total_tokens = _int("total_tokens") or self.last_prompt_tokens + self.last_completion_tokens
        if self._fallback is not None:
            try:
                self._fallback.update_from_response(usage)
            except Exception:
                pass

    def update_model(self, model: str, context_length: int, base_url: str = "", api_key: str = "", provider: str = "", api_mode: str = "") -> None:
        try:
            super().update_model(model, context_length, base_url=base_url, api_key=api_key, provider=provider, api_mode=api_mode)
        except TypeError:
            super().update_model(model, context_length)
        self.threshold_tokens = int(self.context_length * self.threshold_percent) if self.context_length else 0
        self._model = str(model or "")
        self._route = {"base_url": str(base_url or ""), "api_key": str(api_key or ""), "provider": str(provider or ""), "api_mode": str(api_mode or "")}
        self._build_fallback()

    def should_compress(self, prompt_tokens: int = None) -> bool:
        current = self.last_prompt_tokens if prompt_tokens is None else int(prompt_tokens or 0)
        if self.mode == "shadow":
            # Shadow mode observes Jev plans but must not silently disable real
            # context management. At pressure, delegate compression to Hermes'
            # built-in compressor when the fallback is available.
            return bool(self._fallback is not None and self.threshold_tokens and current >= self.threshold_tokens)
        return bool(self.threshold_tokens and current >= self.threshold_tokens)

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        return False

    def _items(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        protected = _protected_indexes(messages, self.protect_first_n, self.protect_last_n)
        calls = _tool_call_map(messages)
        items: list[dict[str, Any]] = []
        index_by_id: dict[str, int] = {}
        for index, message in enumerate(messages):
            if index in protected or not isinstance(message, dict) or message.get("role") != "tool":
                continue
            content = _content_text(message.get("content"))
            if not content:
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            tool_name, args = calls.get(tool_call_id, (str(message.get("name") or ""), {}))
            evidence_id = f"msg-{index}-{tool_call_id or canonical_hash(content)[:10]}"
            items.append(
                {
                    "id": evidence_id,
                    "kind": "tool_result",
                    "content": content,
                    "recoverable": ledger.tool_is_recoverable(tool_name, args),
                    "metadata": {
                        "message_index": index,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "tool_args": args,
                        "recovery_pointer": f"nerve_context_rehydrate:{evidence_id}",
                    },
                }
            )
            index_by_id[evidence_id] = index
        return items, index_by_id

    def _select_candidates(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Select one bounded, progressive batch for automatic semantic curation.

        When eligible evidence exceeds MAX_ITEMS the *most-recent* items are
        selected so that the freshest context is always semantically curated.
        Older, unselected evidence remains byte-for-byte exact in ``messages``;
        it is deferred, not destroyed.
        """
        eligible: list[dict[str, Any]] = []
        skipped_anchors = 0
        skipped_unrecoverable = 0
        for item in items:
            if _is_jev_anchor(item.get("content")):
                skipped_anchors += 1
                continue
            if not bool(item.get("recoverable")):
                skipped_unrecoverable += 1
                continue
            eligible.append(item)
        # Goal-11: select the most-recent eligible items so long sessions always
        # present the freshest evidence to the curation contract.
        selected = eligible[-context.MAX_ITEMS:] if len(eligible) > context.MAX_ITEMS else eligible
        stats = {
            "input_items": len(items),
            "eligible_items": len(eligible),
            "selected_items": len(selected),
            "deferred_items": max(0, len(eligible) - len(selected)),
            "skipped_anchor_items": skipped_anchors,
            "skipped_unrecoverable_items": skipped_unrecoverable,
        }
        return selected, stats

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]:
        if self.mode == "shadow":
            return self._fallback_compress(messages, current_tokens, focus_topic, force, memory_context)
        if not isinstance(messages, list) or not messages:
            return messages

        items, index_by_id = self._items(messages)
        selected, selection = self._select_candidates(items)
        self._last_selection = selection
        if not selected:
            # If tool evidence exists but every item is already anchored or
            # deterministically unrecoverable, preserve it exactly. Delegating
            # that case to a generic summarizer could erase the very evidence
            # this engine's safety policy refused to compact. Text-only sessions
            # still use Hermes' built-in fallback as before.
            if items:
                self._last_plan = {
                    "contract": "context-engine/no-safe-candidates/v1",
                    "stats": {"engine_selection": dict(selection)},
                }
                return messages
            return self._fallback_compress(
                messages, current_tokens, focus_topic, force, memory_context, nerve_candidates=0
            )

        self._curation_attempts += 1
        try:
            plan = context.curate_context(
                goal=_goal(messages, focus_topic),
                items=selected,
                preserve_tail=0,
                mode="apply",
                contract="context-engine/v1",
            )
            if not isinstance(plan, dict):
                raise ValueError("context curation returned a non-object plan")
            plan = dict(plan)
            plan_stats = dict(plan.get("stats") or {}) if isinstance(plan.get("stats"), dict) else {}
            plan_stats["engine_selection"] = dict(selection)
            # Goal-11: expose the candidate cap counters so callers can see how many
            # items were bounded by the long-session working-set limit.
            plan_stats["context_engine_candidates_total"] = selection.get("input_items", len(items))
            plan_stats["context_engine_candidates_selected"] = selection.get("selected_items", len(selected))
            plan_stats["context_engine_candidates_skipped"] = selection.get("deferred_items", 0)
            plan["stats"] = plan_stats
            self._last_plan = plan

            curated_by_id = {
                item["id"]: item for item in plan.get("curated_items", [])
                if isinstance(item, dict) and item.get("id")
            }
            decisions = {
                item["id"]: item for item in plan.get("decisions", [])
                if isinstance(item, dict) and item.get("id")
            }
            selected_by_id = {item["id"]: item for item in selected}
            out = [dict(msg) if isinstance(msg, dict) else msg for msg in messages]
            changed = False
            for evidence_id, original in selected_by_id.items():
                index = index_by_id.get(evidence_id)
                if index is None:
                    continue
                decision = decisions.get(evidence_id) or {}
                action = decision.get("action")
                if action in {"KEEP_EXACT", "PIN", None}:
                    continue
                curated = curated_by_id.get(evidence_id)
                if action == "ANCHOR" and curated:
                    replacement_content = curated.get("content")
                    if isinstance(replacement_content, str) and replacement_content != out[index].get("content"):
                        out[index]["content"] = replacement_content
                        changed = True
                elif action == "DROP":
                    anchor = context._anchor(
                        context.EvidenceItem(
                            id=original["id"], kind=original["kind"], content=original["content"],
                            recoverable=original["recoverable"], pinned=False, metadata=original["metadata"],
                        ),
                        120,
                    )
                    if anchor != out[index].get("content"):
                        out[index]["content"] = anchor
                        changed = True

            if changed:
                self.compression_count += 1
                self.last_prompt_tokens = -1
                return out
        except Exception as exc:
            self._curation_fail_open_count += 1
            self._last_failure_stage = "nerve-curation"
            self._last_failure_type = type(exc).__name__
            self._last_plan = {
                "contract": "context-engine/fail-open/v1",
                "stats": {
                    "nerve_curation_failed": True,
                    "failure_type": self._last_failure_type,
                    # Goal-11: record only the exception type; provider/error text must
                    # not leak into telemetry.
                    "curation_error_type": type(exc).__name__,
                    "engine_selection": dict(selection),
                },
            }
            return self._fallback_compress(
                messages, current_tokens, focus_topic, force, memory_context, len(selected)
            )

        return self._fallback_compress(
            messages, current_tokens, focus_topic, force, memory_context, len(selected)
        )

    def prune_tool_results_only(self, messages: list[dict[str, Any]], current_tokens: int | None = None):
        return messages, 0

    def select_context(self, request_messages: list[dict[str, Any]], **kwargs: Any):
        # Stable/no-op by design: avoid prompt-cache churn. Curation happens only
        # at explicit compression boundaries.
        return None

    def on_turn_complete(self, messages: list[dict[str, Any]], usage: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if self.mode != "shadow" or not self.context_length:
            return None
        prompt = self.last_prompt_tokens
        if usage:
            try:
                prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or prompt or 0)
            except (TypeError, ValueError):
                pass
        if prompt < int(self.context_length * self.shadow_trigger_percent):
            return None

        items, _ = self._items(messages)
        selected, selection = self._select_candidates(items)
        self._last_selection = selection
        if not selected:
            return None
        signature = canonical_hash([(item["id"], item["content"][:120]) for item in selected])
        if signature == self._last_shadow_signature:
            return None
        self._last_shadow_signature = signature
        self._shadow_curation_attempts += 1
        try:
            plan = context.curate_context(
                goal=_goal(messages), items=selected, preserve_tail=0,
                mode="shadow", contract="context-engine-shadow/v1",
            )
            if isinstance(plan, dict):
                plan = dict(plan)
                plan_stats = dict(plan.get("stats") or {}) if isinstance(plan.get("stats"), dict) else {}
                plan_stats["engine_selection"] = dict(selection)
                plan["stats"] = plan_stats
                self._last_plan = plan
        except Exception as exc:
            self._shadow_curation_failures += 1
            self._last_failure_stage = "shadow-curation"
            self._last_failure_type = type(exc).__name__
            self._last_plan = {
                "contract": "context-engine-shadow/fail-open/v1",
                "stats": {
                    "shadow_curation_failed": True,
                    "failure_type": self._last_failure_type,
                    "engine_selection": dict(selection),
                },
            }
        return None

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "")
        ledger.set_session(self._session_id)
        if self._fallback is not None:
            try:
                self._fallback.on_session_start(session_id, **kwargs)
            except Exception:
                pass

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if self._fallback is not None:
            try:
                self._fallback.on_session_end(session_id, messages)
            except Exception:
                pass
        return None

    def on_session_reset(self) -> None:
        try:
            super().on_session_reset()
        except Exception:
            self.last_prompt_tokens = 0
            self.last_completion_tokens = 0
            self.last_total_tokens = 0
            self.compression_count = 0
        if self._fallback is not None:
            try:
                self._fallback.on_session_reset()
            except Exception:
                pass
        lifecycle.reset()
        self._last_shadow_signature = ""
        self._last_plan = {}
        self._last_selection = {}
        self._last_failure_stage = ""
        self._last_failure_type = ""

    def get_status(self) -> dict[str, Any]:
        try:
            status = dict(super().get_status())
        except Exception:
            status = {
                "last_prompt_tokens": max(self.last_prompt_tokens, 0),
                "threshold_tokens": self.threshold_tokens,
                "context_length": self.context_length,
                "compression_count": self.compression_count,
            }
        status.update(
            {
                "engine": self.name,
                "mode": self.mode,
                "shadow_trigger_percent": self.shadow_trigger_percent,
                "fallback_builtin": self.fallback_builtin,
                "fallback_available": self._fallback is not None,
                "ledger": ledger.stats(),
                "context_selection": dict(self._last_selection),
                "fail_open": {
                    "curation_attempts": self._curation_attempts,
                    "curation_fail_open_count": self._curation_fail_open_count,
                    "fallback_fail_open_count": self._fallback_fail_open_count,
                    "last_failure_stage": self._last_failure_stage,
                    "last_failure_type": self._last_failure_type,
                },
                "shadow": {
                    "curation_attempts": self._shadow_curation_attempts,
                    "curation_failures": self._shadow_curation_failures,
                },
                "last_plan_stats": (self._last_plan.get("stats") if isinstance(self._last_plan, dict) else None),
            }
        )
        return status
