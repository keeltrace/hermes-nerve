"""Provider-neutral selective context admission for Nerve v0.3.2.2."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
import hashlib, json, math, re, threading, time
from typing import Any, Callable, Iterable, Mapping, Sequence

_WORD=re.compile(r"[A-Za-z0-9_./:-]+")
_VALID_KINDS={"memory","tool","skill"}
_VALID_MODES={"off","shadow","apply"}

# Low-information request words must not make a tool relevant by themselves.
# This list is intentionally small and domain-neutral: nouns such as ``health``,
# ``budget``, ``issue`` and ``context`` remain meaningful ranking evidence.
_STOPWORDS={
    "a","an","and","are","as","at","be","by","check","current","do","for",
    "explain","find","from","get","give","in","is","it","latest","list","look","me","my","of","on",
    "please","read","running","show","the","to","up","use","using","want","what","with",
}
_GENERIC_TOOL_TERMS={"context","health","issue","project","search","status","tool"}
_TOOL_REQUEST_NOISE={"appropriate","one","once","only","reply","report","sentence","whether"}
_TERM_ALIASES={
    "statistics":"stat","statistic":"stat","stats":"stat",
    "emails":"email","messages":"message","projects":"project","issues":"issue",
    "meetings":"meeting","branches":"branch","processes":"process","plugins":"plugin",
    "files":"file","logs":"log","repositories":"repository",
}

class AdmissionAction(str,Enum):
    PIN="pin"; ADMIT="admit"; DEFER="defer"; SUPPRESS="suppress"

@dataclass(frozen=True)
class ContextCandidate:
    kind:str
    candidate_id:str
    source:str=""
    title:str=""
    summary:str=""
    estimated_tokens:int=0
    metadata:Mapping[str,Any]=field(default_factory=dict)
    provider_score:float|None=None
    trust:float|None=None
    recency:float|None=None
    mandatory:bool=False
    def normalized(self):
        kind=str(self.kind or "").strip().lower()
        if kind not in _VALID_KINDS: raise ValueError(f"unknown context kind: {kind!r}")
        cid=str(self.candidate_id or "").strip()
        if not cid: raise ValueError("candidate_id is required")
        return ContextCandidate(kind,cid,str(self.source or "")[:160],str(self.title or "")[:500],
            str(self.summary or "")[:4000],max(0,int(self.estimated_tokens or 0)),dict(self.metadata or {}),
            _prob(self.provider_score),_prob(self.trust),_prob(self.recency),bool(self.mandatory))

@dataclass(frozen=True)
class AdmissionDecision:
    candidate_id:str
    action:AdmissionAction
    reason_code:str
    confidence:float
    score:float
    lease_id:str=""

@dataclass(frozen=True)
class TaskFingerprint:
    digest:str
    tokens:tuple[str,...]
    project:str=""
    repository:str=""

@dataclass
class AdmissionLease:
    lease_id:str
    task_fingerprint:str
    kind:str
    candidate_ids:tuple[str,...]
    created_at:float
    touched_at:float

@dataclass
class SurfaceConfig:
    mode:str="apply"
    max_candidates:int=48
    max_admitted:int=8
    min_score:float=.28

@dataclass
class SelectiveContextConfig:
    enabled:bool=True
    memory:SurfaceConfig=field(default_factory=lambda:SurfaceConfig("apply",24,8,.28))
    tools:SurfaceConfig=field(default_factory=lambda:SurfaceConfig("apply",128,16,.18))
    skills:SurfaceConfig=field(default_factory=lambda:SurfaceConfig("apply",128,5,.22))
    lease_enabled:bool=True
    lease_max_age_seconds:int=3600
    semantic_enabled:bool=False

def _prob(value):
    if value is None or value=="": return None
    try: return max(0.,min(1.,float(value)))
    except (TypeError,ValueError): return None

def _normalize_term(token):
    token=str(token or "").lower().strip().strip("._/:-")
    if not token or token in _STOPWORDS:
        return ""
    token=_TERM_ALIASES.get(token,token)
    # Conservative singularization improves natural-language/tool-name matching
    # without turning short identifiers into unrelated stems.
    if token.endswith("ies") and len(token)>5:
        token=token[:-3]+"y"
    elif token.endswith("s") and len(token)>4 and not token.endswith(("ss","us","is")):
        token=token[:-1]
    return _TERM_ALIASES.get(token,token)

def _terms(value):
    # Keep the full identifier token AND meaningful delimiter-separated components.
    terms=set()
    for match in _WORD.finditer(str(value or "")):
        raw=match.group(0).lower()
        full=_normalize_term(raw)
        if len(full)>1:
            terms.add(full)
        for part in re.split(r"[_./:-]+", raw):
            part=_normalize_term(part)
            if len(part)>1:
                terms.add(part)
    return terms

def _tool_semantic_key(candidate_id):
    """Collapse only aliases known to expose the same Paperclip capability.

    The complete Hermes catalog remains untouched; this key only prevents two
    equivalent routes from consuming separate HOT slots.
    """
    name=str(candidate_id or "").lower()
    marker="__paperclip_"
    if marker in name:
        return "paperclip_"+name.rsplit(marker,1)[1]
    return name

def estimate_tokens(text:str)->int:
    return max(1,math.ceil(len(str(text or ""))/4)) if text else 0

def task_fingerprint(user_message:str,*,project:str="",repository:str="",recent_topic:str="")->TaskFingerprint:
    terms=sorted(_terms(user_message)|_terms(recent_topic))
    stable={"tokens":terms,"project":str(project or "").strip().lower(),"repository":str(repository or "").strip().lower()}
    digest=hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:24]
    return TaskFingerprint(digest,tuple(terms),stable["project"],stable["repository"])

class LeaseStore:
    def __init__(self):
        self._lock=threading.RLock(); self._leases={}
    def get(self,fingerprint,kind,*,max_age_seconds):
        now=time.time()
        with self._lock:
            lease=self._leases.get((fingerprint,kind))
            if not lease or now-lease.created_at>max(1,int(max_age_seconds)):
                self._leases.pop((fingerprint,kind),None); return None
            lease.touched_at=now; return lease
    def put(self,fingerprint,kind,candidate_ids):
        now=time.time(); payload=f"{fingerprint}:{kind}:{','.join(candidate_ids)}:{int(now)}"
        lease=AdmissionLease("nlease-"+hashlib.sha256(payload.encode()).hexdigest()[:20],fingerprint,kind,tuple(candidate_ids),now,now)
        with self._lock: self._leases[(fingerprint,kind)]=lease
        return lease
    def invalidate(self,*,fingerprint="",kind=""):
        with self._lock:
            keys=[k for k in self._leases if (not fingerprint or k[0]==fingerprint) and (not kind or k[1]==kind)]
            for k in keys:self._leases.pop(k,None)
            return len(keys)
    def count(self):
        with self._lock:return len(self._leases)

class ContextAdmissionController:
    def __init__(self,config=None):
        self.config=config or SelectiveContextConfig(); self.leases=LeaseStore(); self._lock=threading.RLock()
        self._metrics={k:0 for k in ("plans","candidates","admitted","deferred","suppressed","baseline_tokens","selected_tokens","gross_tokens_saved","semantic_calls","semantic_errors","fail_open","lease_hits","lease_invalidations")}

    def _surface(self,kind): return getattr(self.config, {"tool":"tools","skill":"skills"}.get(kind,kind))

    @staticmethod
    def _fallback_action(kind): return AdmissionAction.SUPPRESS if kind=="memory" else AdmissionAction.DEFER

    @staticmethod
    def _mandatory(candidate): return candidate.mandatory or bool(candidate.metadata.get("mandatory"))

    @staticmethod
    def _hard_exclusion(candidate):
        """Return (action, reason, confidence) if candidate is hard-excluded, else None.
        Hard exclusions apply to NON-MANDATORY candidates only."""
        md = candidate.metadata
        if bool(md.get("already_present")):
            return (AdmissionAction.SUPPRESS, "ALREADY_PRESENT", 0.99)
        if bool(md.get("expired")) or md.get("status") in ("expired", "superseded"):
            return (AdmissionAction.SUPPRESS, "STALE", 0.98)
        if bool(md.get("entity_mismatch")):
            return (AdmissionAction.SUPPRESS, "ENTITY_MISMATCH", 0.98)
        return None

    def plan(self,*,goal,candidates,kind,fingerprint=None,semantic_decider=None):
        kind=str(kind or "").strip().lower()
        if kind not in _VALID_KINDS: raise ValueError(f"unknown context kind: {kind}")
        surface=self._surface(kind); mode=surface.mode if surface.mode in _VALID_MODES else "shadow"
        raw=list(candidates); normalized=[]; authorized=[]; by_id={}; baseline_tokens=0
        try:
            # Scan the complete authorized input before any discretionary cap.
            for item in raw:
                c=item if isinstance(item,ContextCandidate) else ContextCandidate(**dict(item))
                c=c.normalized()
                if c.kind!=kind: raise ValueError("candidate kind differs from surface")
                authorized.append(c)
                previous=by_id.get(c.candidate_id)
                if previous is None or (self._mandatory(c) and not self._mandatory(previous)):
                    by_id[c.candidate_id]=c
            normalized=list(by_id.values())
            # Baseline tokens from deduplicated universe
            baseline_tokens=sum(c.estimated_tokens for c in normalized)
        except Exception:
            with self._lock:self._metrics["fail_open"]+=1
            return self._fail_open(kind,mode,raw)
        if not self.config.enabled or mode=="off": return self._fail_open(kind,mode,authorized)

        # Mandatory retention takes precedence, not authority: expose conflicts
        # rather than reclassifying stale/wrong-entity content as trusted/fresh.
        mandatory_decisions={}
        conflict_ids={c.candidate_id for c in authorized if
            any(bool(c.metadata.get(k)) for k in ("already_present","expired","entity_mismatch")) or c.metadata.get("status") in ("expired","superseded")}
        for c in normalized:
            if self._mandatory(c):
                conflict=c.candidate_id in conflict_ids
                mandatory_decisions[c.candidate_id]=AdmissionDecision(c.candidate_id,AdmissionAction.PIN,
                    "MANDATORY_CONFLICT" if conflict else "MANDATORY",1.,1.)
        fp=fingerprint or task_fingerprint(goal)
        lease=self.leases.get(fp.digest,kind,max_age_seconds=self.config.lease_max_age_seconds) if self.config.lease_enabled else None

        # Lease validation: lease is valid only if all leased candidates exist in current universe
        # AND are not hard-excluded (unless mandatory)
        lease_valid = False
        if lease:
            leased_ids = set(lease.candidate_ids)
            if leased_ids.issubset(by_id):
                # Check each leased candidate for hard exclusion (non-mandatory only)
                all_valid = True
                for cid in leased_ids:
                    c = by_id[cid]
                    if not self._mandatory(c):
                        exclusion = self._hard_exclusion(c)
                        if exclusion is not None:
                            all_valid = False
                            break
                if all_valid:
                    lease_valid = True
                else:
                    # Invalidate lease for this fingerprint+kind (hard exclusion)
                    with self._lock:
                        self._metrics["lease_invalidations"] += 1
                    self.leases.invalidate(fingerprint=fp.digest, kind=kind)
                    lease = None  # Force fall-through to normal ranking
            else:
                # Leased candidate disappeared from current universe
                with self._lock:
                    self._metrics["lease_invalidations"] += 1
                self.leases.invalidate(fingerprint=fp.digest, kind=kind)
                lease = None  # Force fall-through to normal ranking

        if lease_valid:
            hot=set(lease.candidate_ids)|set(mandatory_decisions)
            decisions=[mandatory_decisions.get(c.candidate_id) or AdmissionDecision(c.candidate_id,
                        AdmissionAction.PIN if c.candidate_id in hot else self._fallback_action(kind),
                        "SESSION_PIN" if c.candidate_id in hot else "LEASE_DEFER",1.,1. if c.candidate_id in hot else 0.) for c in normalized]
            # Preserve lease lifetime while immediately including new requirements.
            with self.leases._lock:
                lease.candidate_ids=tuple(c.candidate_id for c in normalized if c.candidate_id in hot)
            decisions=[AdmissionDecision(d.candidate_id,d.action,d.reason_code,d.confidence,d.score,
                        lease.lease_id if d.candidate_id in hot else "") for d in decisions]
            with self._lock:self._metrics["lease_hits"]+=1
            return self._result(kind,mode,normalized,decisions,baseline_tokens,fp,normalized)

        goal_terms=_terms(goal); decisions_by_id=dict(mandatory_decisions); scored=[]
        discretionary=0
        for c in normalized:
            if c.candidate_id in mandatory_decisions: continue
            discretionary+=1
            if discretionary>max(0,int(surface.max_candidates)):
                decisions_by_id[c.candidate_id]=AdmissionDecision(c.candidate_id,self._fallback_action(kind),"CANDIDATE_CAP",1.,0.); continue
            # Hard exclusions for non-mandatory candidates
            if not self._mandatory(c):
                exclusion = self._hard_exclusion(c)
                if exclusion is not None:
                    action, reason, confidence = exclusion
                    decisions_by_id[c.candidate_id]=AdmissionDecision(c.candidate_id, action, reason, confidence, 0.); continue
            title_terms=_terms(c.title)
            detail_terms=_terms(c.summary)|_terms(c.metadata.get("keywords",""))
            c_terms=title_terms|detail_terms
            rank_goal_terms=(goal_terms-_TOOL_REQUEST_NOISE) if kind=="tool" else goal_terms
            matched=rank_goal_terms&c_terms
            coverage=len(matched)/max(1,len(rank_goal_terms)) if rank_goal_terms else 0.
            provider=c.provider_score if c.provider_score is not None else 0.
            trust=c.trust if c.trust is not None else .65
            recency=c.recency if c.recency is not None else .65
            if kind=="tool":
                name_coverage=len(rank_goal_terms&title_terms)/max(1,len(rank_goal_terms)) if rank_goal_terms else 0.
                name_precision=len(goal_terms&title_terms)/max(1,len(title_terms)) if title_terms else 0.
                penalty=min(.18,c.estimated_tokens/10000.)
                # Relevance dominates tool ranking. Trust/recency are tie-breakers, not
                # enough by themselves to clear the tool admission threshold.
                relevance=.48*name_coverage+.30*coverage+.12*name_precision
                support=.05*provider+.03*trust+.02*recency
                score=max(0.,min(1.,relevance+support-penalty))
                specific_goal=rank_goal_terms-_GENERIC_TOOL_TERMS
                if specific_goal and not (specific_goal&c_terms) and provider<.55:
                    # Do not route a named-domain request (e.g. Paperclip health)
                    # to an unrelated tool merely because both contain "health".
                    score*=.15
                elif rank_goal_terms and not matched and provider<.55:
                    score*=.10
                confidence=max(coverage,name_coverage,provider,trust*.5)
            else:
                # Preserve established memory/skills semantics.
                penalty=min(.25,c.estimated_tokens/8000.)
                score=max(0.,min(1.,.56*coverage+.16*provider+.18*trust+.10*recency-penalty))
                if goal_terms and coverage==0. and provider<.55:
                    score*=.20
                confidence=max(coverage,provider,trust*.7)
            scored.append((score,c,"DIRECT_TASK_MATCH" if matched else "LOW_RELEVANCE",confidence))
        scored.sort(key=lambda r:(-r[0],r[1].estimated_tokens,r[1].candidate_id))
        room=max(0,int(surface.max_admitted))
        eligible=[r for r in scored if r[0]>=surface.min_score]
        # Relative cutoff keeps obvious tasks compact without imposing an arbitrary
        # tiny cap on genuinely broad requests. A strong leader raises the bar;
        # close-scoring multi-domain tools remain eligible.
        if eligible:
            top=eligible[0][0]
            relative=max(surface.min_score,top*.62,top-.10)
            eligible=[r for r in eligible if r[0]>=relative]
        # Equivalent Paperclip aliases remain available through normal Tool Search,
        # but only the best-scoring route consumes a HOT slot.
        selected=[]; semantic_seen=set()
        for r in eligible:
            key=_tool_semantic_key(r[1].candidate_id) if kind=="tool" else r[1].candidate_id
            if key in semantic_seen:
                continue
            semantic_seen.add(key); selected.append(r)
            if len(selected)>=room:
                break

        if self.config.semantic_enabled and semantic_decider and scored:
            try:
                ambiguous=[r[1] for r in scored[:min(24,len(scored))]]
                valid={c.candidate_id for c in ambiguous}
                ids=[x for x in dict.fromkeys(str(x) for x in semantic_decider(goal,ambiguous,room)) if x in valid][:room]
                if ids:
                    wanted=set(ids); selected=[r for r in scored if r[1].candidate_id in wanted]
                with self._lock:self._metrics["semantic_calls"]+=1
            except Exception:
                with self._lock:self._metrics["semantic_errors"]+=1

        chosen={r[1].candidate_id for r in selected}
        chosen_semantic={_tool_semantic_key(cid) for cid in chosen} if kind=="tool" else set()
        for score,c,reason,confidence in scored:
            alias_deferred=(kind=="tool" and c.candidate_id not in chosen and _tool_semantic_key(c.candidate_id) in chosen_semantic)
            decisions_by_id[c.candidate_id]=AdmissionDecision(c.candidate_id,
                AdmissionAction.ADMIT if c.candidate_id in chosen else self._fallback_action(kind),
                reason if c.candidate_id in chosen else ("SEMANTIC_ALIAS" if alias_deferred else "TOKEN_BUDGET" if score>=surface.min_score else "TOPIC_MISMATCH"),
                confidence if c.candidate_id in chosen else max(.5,1.-score),score)
        decisions=[decisions_by_id[c.candidate_id] for c in normalized]
        hot=[d.candidate_id for d in decisions if d.action in {AdmissionAction.PIN,AdmissionAction.ADMIT}]
        if hot and self.config.lease_enabled:
            lease=self.leases.put(fp.digest,kind,hot)
            decisions=[AdmissionDecision(d.candidate_id,d.action,d.reason_code,d.confidence,d.score,lease.lease_id if d.candidate_id in hot else "") for d in decisions]
        return self._result(kind,mode,normalized,decisions,baseline_tokens,fp,normalized)

    def _fail_open(self,kind,mode,candidates):
        ids=[]; tokens=0
        for i,raw in enumerate(candidates):
            if isinstance(raw,ContextCandidate): cid,cost=raw.candidate_id,raw.estimated_tokens
            elif isinstance(raw,Mapping): cid,cost=str(raw.get("candidate_id") or raw.get("id") or f"candidate-{i}"),raw.get("estimated_tokens") or 0
            else: cid,cost=f"candidate-{i}",0
            try: cost=max(0,int(cost or 0))
            except (TypeError,ValueError,OverflowError): cost=0
            ids.append(cid);tokens+=cost
        return {"schema":"hermes-nerve-selective-context/v1","kind":kind,"mode":mode,"fail_open":True,
            "decisions":[{**asdict(AdmissionDecision(x,AdmissionAction.ADMIT,"FAIL_OPEN",1.,1.)),"action":"admit"} for x in ids],
            "selected_ids":ids,"deferred_ids":[],"suppressed_ids":[],"effective_selected_ids":ids,
            "baseline_tokens":tokens,"selected_tokens":tokens,"effective_tokens":tokens,
            "gross_tokens_saved":0,"applied_tokens_saved":0,"admission_overhead_tokens":0,"net_tokens_saved":0}

    def _result(self,kind,mode,candidates,decisions,baseline_tokens,fp,authorized=None):
        authorized=candidates if authorized is None else authorized
        selected=[d.candidate_id for d in decisions if d.action in {AdmissionAction.PIN,AdmissionAction.ADMIT}]
        deferred=[d.candidate_id for d in decisions if d.action==AdmissionAction.DEFER]
        suppressed=[d.candidate_id for d in decisions if d.action==AdmissionAction.SUPPRESS]
        selected_set=set(selected)
        selected_tokens=sum(c.estimated_tokens for c in authorized if c.candidate_id in selected_set); gross=max(0,baseline_tokens-selected_tokens)
        all_ids=[c.candidate_id for c in authorized]
        effective_selected=selected if mode=="apply" else all_ids
        effective_tokens=selected_tokens if mode=="apply" else baseline_tokens
        applied=max(0,baseline_tokens-effective_tokens)
        with self._lock:
            self._metrics["plans"]+=1;self._metrics["candidates"]+=len(candidates);self._metrics["admitted"]+=len(selected)
            self._metrics["deferred"]+=len(deferred);self._metrics["suppressed"]+=len(suppressed)
            self._metrics["baseline_tokens"]+=baseline_tokens;self._metrics["selected_tokens"]+=selected_tokens;self._metrics["gross_tokens_saved"]+=gross
        return {"schema":"hermes-nerve-selective-context/v1","kind":kind,"mode":mode,"fail_open":False,"task_fingerprint":fp.digest,
            "decisions":[{**asdict(d),"action":d.action.value} for d in decisions],"selected_ids":selected,"deferred_ids":deferred,
            "suppressed_ids":suppressed,"effective_selected_ids":effective_selected,
            "baseline_tokens":baseline_tokens,"selected_tokens":selected_tokens,"effective_tokens":effective_tokens,
            "gross_tokens_saved":gross,"applied_tokens_saved":applied,"admission_overhead_tokens":0,"net_tokens_saved":applied}

    def report(self):
        with self._lock: metrics=dict(self._metrics)
        base=int(metrics["baseline_tokens"]);gross=int(metrics["gross_tokens_saved"])
        return {"schema":"hermes-nerve-selective-context-stats/v1","enabled":bool(self.config.enabled),
            "modes":{"memory":self.config.memory.mode,"tools":self.config.tools.mode,"skills":self.config.skills.mode},
            "metrics":metrics,"lease_count":self.leases.count(),"gross_reduction_percent":round(100.*gross/base,3) if base else 0.,
            "semantic_provider_calls":int(metrics["semantic_calls"])}

def memory_candidate_from_experience(item):
    claim=dict(item.get("claim") or item);statement=str(claim.get("statement") or "");kind=str(claim.get("claim_type") or "")
    trust={"verified":1.,"disproven":.95,"observed":.75,"inferred":.5}.get(kind,.5)
    return ContextCandidate("memory",str(claim.get("claim_id") or item.get("candidate_id") or ""),"nerve_verified_experience",
        str(claim.get("subject") or ""),statement,int(claim.get("estimated_tokens") or estimate_tokens(statement)),
        {"status":str(claim.get("status") or "active"),"evidence_ids":list(claim.get("evidence_ids") or []),"claim_type":kind},
        _prob(item.get("score")),trust,None,bool(claim.get("mandatory")))

def tool_candidate(tool):
    fn=tool.get("function") if isinstance(tool.get("function"),Mapping) else tool
    name=str(fn.get("name") or tool.get("name") or "");desc=str(fn.get("description") or tool.get("description") or "")
    schema=json.dumps(tool,sort_keys=True,separators=(",",":"),default=str)
    return ContextCandidate("tool",name,str(tool.get("source") or "hermes"),name,desc,
        int(tool.get("estimated_tokens") or estimate_tokens(schema)),{"toolset":tool.get("toolset",""),"core":bool(tool.get("core"))},
        None,None,None,bool(tool.get("mandatory") or tool.get("core")))

def skill_candidate(skill):
    name=str(skill.get("name") or skill.get("id") or "");desc=str(skill.get("description") or "")
    return ContextCandidate("skill",name,str(skill.get("source") or "hermes"),name,desc,
        int(skill.get("estimated_tokens") or estimate_tokens(f"{name}: {desc}")),{"category":skill.get("category","")},
        None,None,None,bool(skill.get("mandatory")))

_default=ContextAdmissionController()

def configure(**kwargs):
    global _default
    _default=ContextAdmissionController(SelectiveContextConfig(
        enabled=bool(kwargs.get("enabled",True)),
        memory=SurfaceConfig(str(kwargs.get("memory_mode","apply")).lower(),max(1,int(kwargs.get("memory_max_candidates",24))),max(0,int(kwargs.get("memory_max_admitted",8))),float(kwargs.get("memory_min_score",.28))),
        tools=SurfaceConfig(str(kwargs.get("tools_mode","apply")).lower(),max(1,int(kwargs.get("tools_max_candidates",128))),max(0,int(kwargs.get("tools_max_admitted",16))),float(kwargs.get("tools_min_score",.18))),
        skills=SurfaceConfig(str(kwargs.get("skills_mode","apply")).lower(),max(1,int(kwargs.get("skills_max_candidates",128))),max(0,int(kwargs.get("skills_max_admitted",5))),float(kwargs.get("skills_min_score",.22))),
        lease_enabled=bool(kwargs.get("lease_enabled",True)),lease_max_age_seconds=max(1,int(kwargs.get("lease_max_age_seconds",3600))),
        semantic_enabled=bool(kwargs.get("semantic_enabled",False))))

def plan_memory(goal,candidates,**kwargs): return _default.plan(goal=goal,candidates=candidates,kind="memory",**kwargs)
def plan_tools(goal,candidates,**kwargs): return _default.plan(goal=goal,candidates=candidates,kind="tool",**kwargs)
def plan_skills(goal,candidates,**kwargs): return _default.plan(goal=goal,candidates=candidates,kind="skill",**kwargs)
def report(): return _default.report()