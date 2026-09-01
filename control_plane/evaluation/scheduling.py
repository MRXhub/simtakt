"""Pure, explicit scheduling decisions and adaptive compute-profile ranking."""
from __future__ import annotations
import hashlib,json,math,re
from collections.abc import Mapping,Sequence,Set
from datetime import datetime,timezone
from typing import Any
from control_plane.core.evaluation_contracts import canonical_json
from control_plane.evaluation.execution_options import (ExecutionOptionError,validate_execution_option,validate_execution_option_set,validate_parallel_efficiency_calibration,validate_performance_profile,validate_performance_profile_snapshot)
from control_plane.evaluation.compute_profile import (DEFAULT_MIN_SAMPLES,PRESSURE_CAP,ComputeProfileError,estimate_shape,validate_capacity_profile_snapshot,validate_task_class,validate_task_override)
_REVISION=re.compile(r"^sha256:[0-9a-f]{64}$"); _EVALUATION_ID=re.compile(r"^evaluation:[A-Za-z0-9._:-]+$"); _OPTION_SET_ID=re.compile(r"^execution-option-set:sha256:[0-9a-f]{64}$"); _PROFILE_SNAPSHOT_ID=re.compile(r"^performance-profile-snapshot:sha256:[0-9a-f]{64}$"); _RUN_ID=re.compile(r"^\d{8}-\d{6}-\d{3}$"); _POSIX_PATH=re.compile(r"^/[A-Za-z0-9._/-]+$")
_OPTION_POLICIES=frozenset({"throughput","latency"}); _US=1_000_000; _PPM=1_000_000; SCARCITY_SECONDS_PER_EXTRA_PROCESSOR_AT_FULL_PRESSURE=20
class SchedulingError(ValueError): pass
def _text(v,l):
    x=str(v).strip()
    if not x: raise SchedulingError(f"{l} is required")
    return x
def _nonneg(v,l):
    if isinstance(v,bool) or not isinstance(v,int) or v<0: raise SchedulingError(f"{l} must be a nonnegative integer")
    return v
def _pos(v,l):
    x=_nonneg(v,l)
    if x==0: raise SchedulingError(f"{l} must be a positive integer")
    return x
def _copy(v): return json.loads(canonical_json(dict(v)))
def _rev(v,l):
    x=str(v).strip().lower()
    if not _REVISION.fullmatch(x): raise SchedulingError(f"{l} must be a SHA-256 revision")
    return x
def _cid(p,v): return f"{p}:sha256:"+hashlib.sha256(canonical_json(dict(v)).encode()).hexdigest()
def _time(v,l):
    if isinstance(v,datetime): d=v
    elif isinstance(v,str) and v.strip():
        try:d=datetime.fromisoformat(v.strip().replace("Z","+00:00"))
        except ValueError as e: raise SchedulingError(f"{l} must be a timezone-aware timestamp") from e
    else: raise SchedulingError(f"{l} must be a timezone-aware timestamp")
    if d.tzinfo is None: raise SchedulingError(f"{l} must be a timezone-aware timestamp")
    d=d.astimezone(timezone.utc); return d.isoformat(timespec="microseconds"),d
def _policy(v):
    if not isinstance(v,Mapping) or set(v)!={"priority_order","default_priority","aging_quantum_seconds"}: raise SchedulingError("scheduling_policy must be an object")
    p=v["priority_order"]
    if isinstance(p,(str,bytes,bytearray)) or not isinstance(p,Sequence) or not p: raise SchedulingError("scheduling_policy.priority_order must be a non-empty array")
    p=[_text(x,"scheduling_policy.priority_order item") for x in p]
    if len(p)!=len(set(p)): raise SchedulingError("scheduling_policy.priority_order must be unique")
    d=_text(v["default_priority"],"scheduling_policy.default_priority").lower()
    if d not in p: raise SchedulingError("scheduling_policy.default_priority must belong to priority_order")
    return {"priority_order":p,"default_priority":d,"aging_quantum_seconds":_pos(v["aging_quantum_seconds"],"scheduling_policy.aging_quantum_seconds")}
def _provenance(v):
    if not isinstance(v,Mapping) or set(v)!={"artifact_id","revision","project_state_revision"}: raise SchedulingError("scheduling policy provenance is invalid")
    r={k:_text(v[k],f"scheduling policy provenance {k}") for k in v}
    if not r["artifact_id"].startswith("configuration.project-scheduling-policy.") or any(not _REVISION.fullmatch(r[k].lower()) for k in ("revision","project_state_revision")): raise SchedulingError("scheduling policy provenance is invalid")
    return r
def _candidate(v):
    if not isinstance(v,Mapping): raise SchedulingError("candidate must be an object")
    lf={"attempt_id","target_id","requested_processors","requested_memory_bytes"}
    if set(v)==lf:return {"attempt_id":_text(v["attempt_id"],"candidate.attempt_id"),"target_id":_text(v["target_id"],"candidate.target_id"),"requested_processors":_pos(v["requested_processors"],"candidate.requested_processors"),"requested_memory_bytes":_pos(v["requested_memory_bytes"],"candidate.requested_memory_bytes")}
    prep={"attempt_id","execution_option_set","performance_profile_snapshot"}; ranked={"evaluation_id","priority","queued_since"}; base=set(v)-{"task_class"}
    if base not in (prep,prep|{"calibration"},prep|ranked,prep|ranked|{"calibration"}): raise SchedulingError("candidate contains missing or unknown fields")
    try:
        opts=validate_execution_option_set(v["execution_option_set"]); prof=validate_performance_profile_snapshot(v["performance_profile_snapshot"])
        if "calibration" in v: cal=validate_parallel_efficiency_calibration(v["calibration"])
        else: cal=None
    except ExecutionOptionError as e: raise SchedulingError("candidate execution preparation is invalid") from e
    ob={x["option_id"]:x for x in opts["options"]}; pb={x["execution_option_id"]:x for x in prof["profiles"]}
    if set(ob)!=set(pb) or any(ob[k]["performance_class_id"]!=pb[k]["performance_class_id"] for k in ob): raise SchedulingError("candidate performance profiles do not cover its options")
    r={"attempt_id":_text(v["attempt_id"],"candidate.attempt_id"),"execution_option_set":opts,"performance_profile_snapshot":prof}
    if cal is not None:r["calibration"]=cal
    if "evaluation_id" in v:
        eid=_text(v["evaluation_id"],"candidate.evaluation_id")
        if not _EVALUATION_ID.fullmatch(eid):raise SchedulingError("candidate.evaluation_id is invalid")
        qs,_=_time(v["queued_since"],"candidate.queued_since");r.update({"evaluation_id":eid,"priority":_text(v["priority"],"candidate.priority").lower(),"queued_since":qs})
    if "task_class" in v:
        try:r["task_class"]=validate_task_class(v["task_class"])
        except ComputeProfileError as e:raise SchedulingError("candidate task_class is invalid") from e
    return r
def _active(v):
    if not isinstance(v,Mapping) or set(v) not in ({"attempt_id","target_id","processors","memory_bytes","resource_key"},{"attempt_id","target_id","processors","memory_bytes","resource_key","exclusive_target"}):raise SchedulingError("active allocation contains missing or unknown fields")
    r={"attempt_id":_text(v["attempt_id"],"allocation.attempt_id"),"target_id":_text(v["target_id"],"allocation.target_id"),"processors":_pos(v["processors"],"allocation.processors"),"memory_bytes":_pos(v["memory_bytes"],"allocation.memory_bytes"),"resource_key":_text(v["resource_key"],"allocation.resource_key")}
    if "exclusive_target" in v:
        if not isinstance(v["exclusive_target"],bool):raise SchedulingError("allocation.exclusive_target must be a boolean")
        r["exclusive_target"]=v["exclusive_target"]
    return r
def _resources(v):
    if not isinstance(v,Mapping):raise SchedulingError("resource snapshot must be an object")
    if v.get("status") not in {"ready","blocked"}:raise SchedulingError("resource snapshot status must be ready or blocked")
    obs=v.get("observed_allocation_keys"); reasons=v.get("reasons")
    if not isinstance(obs,list) or len(obs)!=len(set(obs)) or any(not isinstance(x,str) or not x.strip() for x in obs):raise SchedulingError("observed_allocation_keys must be unique strings")
    if not isinstance(reasons,list) or any(not isinstance(x,str) or not x.strip() for x in reasons):raise SchedulingError("resource snapshot reasons must be strings")
    usage=v.get("license_sessions_in_use")
    if usage is not None and (isinstance(usage,bool) or not isinstance(usage,int) or usage<0):raise SchedulingError("resource snapshot license_sessions_in_use must be a nonnegative integer or null")
    return {"schema_version":1,"snapshot_kind":"resource-snapshot","snapshot_revision":_rev(v.get("snapshot_revision"),"resource snapshot revision"),"target_id":_text(v.get("target_id"),"resource snapshot target_id"),"status":v["status"],"available_processors":_nonneg(v["available_processors"],"resource snapshot available_processors"),"available_memory_bytes":_nonneg(v["available_memory_bytes"],"resource snapshot available_memory_bytes"),"default_request_memory_bytes":_pos(v["default_request_memory_bytes"],"resource snapshot default_request_memory_bytes"),"observed_allocation_keys":list(obs),"reasons":list(reasons),**({"target_is_idle":v["target_is_idle"]} if "target_is_idle" in v else {}),**({"license_sessions_in_use":usage} if "license_sessions_in_use" in v else {})}
def _pressure(cs,res,tp,tm):
    d=0
    for c in cs:
        if "execution_option_set" not in c:
            if c["target_id"]==res["target_id"] and c["requested_processors"]<=tp and c["requested_memory_bytes"]<=tm:d+=c["requested_processors"]
        else:
            es=[o for o in c["execution_option_set"]["options"] if o["target_id"]==res["target_id"] and o["processors"]<=tp and o["memory_bytes"]<=tm]
            if c.get("calibration"):es=[o for o in es if o["processors"]==c["calibration"]["selected_processors"]]
            if es:d+=min(o["processors"] for o in es)
    return min(1.0,d/max(1,tp))
def _score(opt,est,base,pressure):
    wall=math.ceil(float(est["estimate_seconds"])*_US); success=math.ceil(float(est["success_estimate"])*_PPM); pp=math.ceil(max(0,min(1,pressure))*_PPM); penalty=20*_US*pp*max(0,opt["processors"]-base["processors"])//_PPM; fail=1 if success==0 else 0; comp=wall if fail else math.ceil(wall*_PPM/success); total=comp+penalty; key=[fail,total,int(opt["processors"]),opt["option_id"]]
    return {"option_id":opt["option_id"],"processors":int(opt["processors"]),"wall_estimate_seconds":float(est["estimate_seconds"]),"success_estimate_ppm":success,"source":str(est["source"]),"fallback_reason":est["fallback_reason"],"pressure_ppm":pp,"base_completion_microseconds":comp,"scarcity_penalty_microseconds":penalty,"total_score_microseconds":total,"failure_rank":fail,"choice_key":key}
def _choose(opts,profiles,shapes,tc,target,rev,pressure,override):
    unc=None if override is None else override["max_uncertainty"]; estimates={}
    for o in opts:
        shape=None if tc is None else shapes.get((tc["key"],target,rev,int(o["processors"])))
        estimates[o["option_id"]]=estimate_shape(profiles[o["option_id"]],shape,min_samples=DEFAULT_MIN_SAMPLES,max_uncertainty=unc)
    base=min(opts,key=lambda o:(o["processors"],o["option_id"])); records={o["option_id"]:_score(o,estimates[o["option_id"]],base,pressure) for o in opts}; chosen=min(opts,key=lambda o:records[o["option_id"]]["choice_key"]); ev=sorted(records.values(),key=lambda x:(x["processors"],x["option_id"]));return chosen,profiles[chosen["option_id"]],records[chosen["option_id"]],ev

def _core(candidates,active_allocations,resource_snapshot,*,option_policy="throughput",scheduling_policy=None,decision_time=None,capacity_profile_snapshot=None,overrides=(),scheduling_policy_provenance=None,capacity_scope=None):
    policyopt=str(option_policy).strip().lower()
    if policyopt not in _OPTION_POLICIES:raise SchedulingError("option_policy must be throughput or latency")
    cs=[_candidate(x) for x in candidates]; aa=[_active(x) for x in active_allocations]; res=_resources(resource_snapshot)
    if len({x["attempt_id"] for x in cs})!=len(cs):raise SchedulingError("candidate Attempt IDs must be unique")
    if len({x["attempt_id"] for x in aa})!=len(aa):raise SchedulingError("active allocation Attempt IDs must be unique")
    prep=any("execution_option_set" in x for x in cs)
    if prep and not all("execution_option_set" in x for x in cs):raise SchedulingError("legacy and prepared execution candidates cannot be mixed")
    ranked=any("evaluation_id" in x for x in cs)
    if ranked and not all("evaluation_id" in x for x in cs):raise SchedulingError("ranked and unranked prepared candidates cannot be mixed")
    pol=prov=dt=nt=None
    if ranked:
        pol=_policy(scheduling_policy);prov=_provenance(scheduling_policy_provenance) if scheduling_policy_provenance is not None else None;nt,dt=_time(decision_time,"decision_time");cs.sort(key=lambda x:x["attempt_id"])
        for c in cs:
            if c["priority"] not in pol["priority_order"]:c["priority"]=pol["default_priority"]
            if _time(c["queued_since"],"candidate.queued_since")[1]>dt:raise SchedulingError("candidate.queued_since cannot follow decision_time")
    elif scheduling_policy is not None or decision_time is not None:raise SchedulingError("scheduling_policy and decision_time apply only to ranked prepared candidates")
    elif scheduling_policy_provenance is not None:raise SchedulingError("scheduling policy provenance applies only to ranked prepared candidates")
    profile=None
    if capacity_profile_snapshot is not None:
        if not ranked:raise SchedulingError("capacity profile snapshots require ranked prepared candidates")
        if scheduling_policy_provenance is None:raise SchedulingError("capacity profile snapshots require scheduling policy provenance")
        try:profile=validate_capacity_profile_snapshot(capacity_profile_snapshot)
        except ComputeProfileError as e:raise SchedulingError("capacity profile snapshot is invalid") from e
    if overrides and profile is None:raise SchedulingError("task overrides require a capacity profile snapshot")
    ovs={}
    for x in overrides:
        try:o=validate_task_override(x)
        except ComputeProfileError as e:raise SchedulingError("task override is invalid") from e
        if o["task_class_key"] in ovs:raise SchedulingError("task overrides must be unique per task class")
        ovs[o["task_class_key"]]=o
    shapes={} if profile is None else {(x["task_class_key"],x["target_id"],x["profile_revision"],int(x["processors"])):x for x in profile["shapes"]}
    snap={"candidates":cs,"active_allocations":aa,"resource_snapshot":res}
    if prep:snap["option_policy"]=policyopt
    if ranked:snap.update({"scheduling_policy":pol,"decision_time":nt});
    if ranked and prov is not None:snap["scheduling_policy_provenance"]=prov
    if profile is not None:snap["capacity_profile_snapshot"]=profile
    if ovs:snap["overrides"]=[ovs[k] for k in sorted(ovs)]
    sr="sha256:"+hashlib.sha256(canonical_json(snap).encode()).hexdigest(); scope=set(capacity_scope) if capacity_scope is not None else {res["target_id"]}; obs=set(res["observed_allocation_keys"]); scoped=[x for x in aa if x["target_id"] in scope]; unseen=[x for x in scoped if x["resource_key"] not in obs]; ap=max(0,res["available_processors"]-sum(x["processors"] for x in unseen));am=max(0,res["available_memory_bytes"]-sum(x["memory_bytes"] for x in unseen)); active=[x for x in scoped if x["target_id"]==res["target_id"]];tp=res["available_processors"]+sum(x["processors"] for x in scoped);tm=res["available_memory_bytes"]+sum(x["memory_bytes"] for x in scoped)
    exclusive=[x for x in cs if x.get("calibration",{}).get("target_isolation")=="exclusive"]; consider=cs; deferred=[]; isolation=None
    if any(x.get("exclusive_target") for x in scoped):isolation="target-isolation-active-calibration"
    elif exclusive and scoped:isolation="target-isolation-awaiting-idle-target"
    elif exclusive and res.get("target_is_idle") is not True:isolation="target-isolation-idle-not-attested"
    elif exclusive:
        consider=exclusive;ids={x["attempt_id"] for x in exclusive};deferred=[{"attempt_id":x["attempt_id"],"reason_code":"target-isolation-reserved"} for x in cs if x["attempt_id"] not in ids]
    if isolation:consider=[];deferred=[{"attempt_id":x["attempt_id"],"reason_code":isolation} for x in cs]
    pressure=_pressure(consider,res,tp,tm) if prep else 0.0; adaptive=ranked and profile is not None and prov is not None; analyses={};selected=selected_option=selected_profile=None;setid=pid=None;considered=list(deferred);ranked_items=[]
    if res["status"]=="ready":
        for c in consider:
            opts=c.get("execution_option_set")
            if opts is None:
                why="target-not-in-snapshot" if c["target_id"]!=res["target_id"] else "insufficient-processors" if c["requested_processors"]>ap else "insufficient-memory" if c["requested_memory_bytes"]>am else "selected"
                if why=="selected":selected=c
            else:
                cps=c["performance_profile_snapshot"]; pb={x["execution_option_id"]:x for x in cps["profiles"]};tc=c.get("task_class");ov=ovs.get(tc["key"]) if profile is not None and tc is not None else None;to=[x for x in opts["options"] if x["target_id"]==res["target_id"]]; cal=c.get("calibration")
                if cal is not None:to=[x for x in to if x["processors"]==cal["selected_processors"]]
                fit=[x for x in to if x["processors"]<=ap and x["memory_bytes"]<=am]
                if fit:
                    rev=tc["boundary"]["simulation_definition"]["revision"] if tc is not None else cps["profile_snapshot_id"];eff=pressure if ov is None else max(0,pressure*(1-float(ov["latency_bias"])));opt,prof,score,ev=_choose(fit,pb,shapes,tc,res["target_id"],rev,eff,ov)
                    if profile is None and tc is None and cal is None and not ranked:
                        def oldkey(o):
                            p=pb[o["option_id"]]
                            return ((p["duration_p50_seconds"]*o["processors"]*1_000_000/p["success_rate_ppm"],p["duration_p90_seconds"],p["duration_p50_seconds"],o["processors"],o["memory_bytes"],o["option_id"]) if policyopt=="throughput" else (p["duration_p50_seconds"],p["duration_p90_seconds"],o["processors"],o["memory_bytes"],o["option_id"]))
                        opt=min(fit,key=oldkey);prof=pb[opt["option_id"]]
                        core_score=math.ceil(prof["duration_p50_seconds"]*opt["processors"]*_US/prof["success_rate_ppm"])
                        score={**score,"total_score_microseconds":core_score,"base_completion_microseconds":core_score,"choice_key":[0,core_score,opt["processors"],opt["option_id"]]}
                    if adaptive:analyses[c["attempt_id"]]={"attempt_id":c["attempt_id"],"task_class":tc,"profile_revision":rev,"applied_override":ov,"selected_option_id":opt["option_id"],"selected_score_record":score,"ranking_choice_key":score["choice_key"],"options":ev}
                    if ranked:
                        q=_time(c["queued_since"],"candidate.queued_since")[1]; wait=int((dt-q).total_seconds());ranked_items.append(((pol["priority_order"].index(c["priority"]),-(wait//pol["aging_quantum_seconds"]),*score["choice_key"],-wait,c["queued_since"],c["attempt_id"]),c,opt,prof));why="lower-scheduling-rank"
                    else:selected,selected_option,selected_profile=c,opt,prof;setid=opts["option_set_id"];pid=cps["profile_snapshot_id"];why="selected"
                else:why="target-not-in-snapshot" if not to else "insufficient-processors" if all(x["processors"]>ap for x in to) else "insufficient-memory"
            considered.append({"attempt_id":c["attempt_id"],"reason_code":why})
            if selected is not None and not ranked:break
    if ranked_items:
        _,selected,selected_option,selected_profile=min(ranked_items,key=lambda x:x[0]);setid=selected["execution_option_set"]["option_set_id"];pid=selected["performance_profile_snapshot"]["profile_snapshot_id"]
        for x in considered:
            if x["attempt_id"]==selected["attempt_id"]:x["reason_code"]="selected"
    body={"schema_version":2 if prep else 1,"decision_kind":"scheduling-decision","snapshot_revision":sr,"resource_snapshot_revision":res["snapshot_revision"],"action":"launch" if selected is not None else "wait","selected_attempt_id":None if selected is None else selected["attempt_id"],"selected_target_id":res["target_id"] if selected is not None else None,"allocation":None if selected is None else {"processors":selected["requested_processors"] if selected_option is None else selected_option["processors"],"memory_bytes":selected["requested_memory_bytes"] if selected_option is None else selected_option["memory_bytes"]},"reason_code":"resources-available" if selected is not None else ("resource-snapshot-blocked" if res["status"]=="blocked" else isolation or "no-candidate-fits"),"considered":considered}
    if adaptive:body["capacity_analysis"]={"schema_version":2,"snapshot_revision":sr,"queue_pressure":pressure,"pressure_basis":"target-total-envelope-minimum-demand","candidates":[analyses[k] for k in sorted(analyses)]};body["scheduling_policy_provenance"]=prov
    if prep:body.update({"option_policy":policyopt,"selected_execution_option_set_id":setid,"selected_execution_option":selected_option,"selected_performance_profile_snapshot_id":pid,"selected_performance_profile":selected_profile})
    return {**body,"decision_id":_cid("scheduling-decision",body)}

class _ImmutableDict(dict):
    def _blocked(self,*a,**k):raise TypeError("immutable scheduling decision")
    __setitem__=__delitem__=clear=pop=popitem=setdefault=update=_blocked
def _plain(v):
    if isinstance(v,Mapping):return {str(k):_plain(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [_plain(x) for x in v]
    return v
def _freeze(v):
    if isinstance(v,Mapping):return _ImmutableDict({k:_freeze(x) for k,x in v.items()})
    if isinstance(v,(list,tuple)):return tuple(_freeze(x) for x in v)
    return v
def scheduling_decision_plain(v):return _plain(v)
def _env(v):
    if v is None:return None
    if not isinstance(v,Mapping) or not {"processors","memory_bytes","license_sessions"}.issubset(v):raise SchedulingError("capacity_envelope requires processors, memory_bytes, and license_sessions")
    if set(v)-{"processors","memory_bytes","license_sessions","license_reserve","baseline_processors","baseline_memory_bytes"}:raise SchedulingError("capacity_envelope contains unknown fields")
    for k in ("processors","memory_bytes","license_sessions"):
        if isinstance(v[k],bool) or not isinstance(v[k],int) or v[k]<1:raise SchedulingError(f"capacity_envelope.{k} must be a positive integer")
    reserve=v.get("license_reserve",0)
    if isinstance(reserve,bool) or not isinstance(reserve,int) or reserve<0 or reserve>=v["license_sessions"]:raise SchedulingError("capacity_envelope.license_reserve must be a nonnegative integer less than license_sessions")
    return {k:v[k] for k in ("processors","memory_bytes","license_sessions")}|{"license_reserve":reserve}
def _capacity_scope(value, target_id):
    if value is None:
        return {target_id}
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (Sequence, Set)):
        raise SchedulingError("capacity_scope must be a set or sequence of target IDs")
    scope=set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SchedulingError("capacity_scope target IDs must be non-empty strings")
        scope.add(item.strip())
    if target_id not in scope:
        raise SchedulingError("capacity_scope must include resource snapshot target_id")
    return scope

def _run(candidates,active,resources,*,legacy=False,capacity_envelope=None,capacity_scope=None,**kw):
    ids=[x.get("attempt_id") for x in candidates if isinstance(x,Mapping)]; aids=[x.get("attempt_id") for x in active if isinstance(x,Mapping)]
    if set(ids)&set(aids):raise SchedulingError("candidate Attempt ID is already present among active allocations")
    env=_env(capacity_envelope);res=dict(resources);scope=_capacity_scope(capacity_scope,res.get("target_id"));on=[x for x in active if isinstance(x,Mapping) and isinstance(x.get("target_id"),str) and x.get("target_id") in scope]
    keys=[(x.get("target_id"),x.get("resource_key")) for x in on if isinstance(x,Mapping)]
    if len(keys)!=len(set(keys)):raise SchedulingError("active allocations contain duplicate (target_id, resource_key)")
    if env is not None:
        res["available_processors"]=min(int(res["available_processors"]),max(0,env["processors"]-sum(int(x["processors"]) for x in on)));res["available_memory_bytes"]=min(int(res["available_memory_bytes"]),max(0,env["memory_bytes"]-sum(int(x["memory_bytes"]) for x in on)))
        if env["license_sessions"]-env["license_reserve"]<=len(active):res["status"]="blocked";res["reasons"]=list(dict.fromkeys([*res.get("reasons",[]),"insufficient-license-sessions"]))
    res["observed_allocation_keys"]=sorted(res.get("observed_allocation_keys",()))
    # The retired prebound dispatcher supplies the historical v1 snapshot
    # shape, which predates attestation fields. Keep fail-closed attestation
    # checks on the new scheduler while preserving legacy launch decisions.
    if res.get("status")=="ready" and not legacy:
        reasons=list(res.get("reasons",[]))
        if not res.get("created_at"):reasons.append("resource-snapshot-unattested")
        if res.get("lock_held") is not True:reasons.append("resource-snapshot-lock-unattested")
        blocking_reasons=[reason for reason in reasons if reason != "license-usage-unparsed"]
        if blocking_reasons:res["status"]="blocked";res["reasons"]=list(dict.fromkeys(reasons))
    ordered=sorted(active,key=lambda x:json.dumps(_plain(x),sort_keys=True,separators=(",",":")))
    result=_core(candidates,ordered,res,capacity_scope=scope,**kw)
    usage_exhausted = env is not None and res.get("license_sessions_in_use") is not None and res["license_sessions_in_use"] >= env["license_sessions"]
    ledger_exhausted = env is not None and env["license_sessions"]-env["license_reserve"]<=len(active)
    if usage_exhausted or ledger_exhausted:
        reason = "license-pool-exhausted" if usage_exhausted else "insufficient-license-sessions"
        p=_plain(result);p.update({"action":"wait","selected_attempt_id":None,"selected_target_id":None,"allocation":None,"reason_code":reason,"considered":[{"attempt_id":str(x.get("attempt_id")),"reason_code":reason} for x in candidates]})
        if p.get("schema_version")==2:
            for k in ("selected_execution_option_set_id","selected_execution_option","selected_performance_profile_snapshot_id","selected_performance_profile"):p[k]=None
        b=dict(p);b.pop("decision_id",None);p["decision_id"]=_cid("scheduling-decision",b);result=p
    return _freeze(result)
def schedule(candidates,active_allocations,resource_snapshot,*,option_policy="throughput",scheduling_policy=None,decision_time=None,capacity_envelope=None,capacity_profile_snapshot=None,overrides=(),scheduling_policy_provenance=None,capacity_scope=None):
    """Return a pure scheduling decision from a full-target active allocation snapshot.

    CPU and memory capacity, resource-key reservations, and target isolation use
    allocations whose ``target_id`` belongs to ``capacity_scope``.  If omitted,
    the scope is the resource snapshot target, preserving the single-target
    behavior.  The ``license_sessions`` capacity envelope is global: every
    active allocation in the input consumes one license session, regardless of
    target.
    """
    flags=[isinstance(x,Mapping) and {"target_id","requested_processors","requested_memory_bytes"}.issubset(x) for x in candidates]
    if any(flags):
        if not all(flags):raise SchedulingError("legacy candidate cannot be mixed with prepared candidates")
        raise SchedulingError("schedule requires a prepared execution option set and frozen performance profile; legacy candidate is accepted only by schedule_legacy_v1")
    return _run(candidates,active_allocations,resource_snapshot,option_policy=option_policy,scheduling_policy=scheduling_policy,decision_time=decision_time,capacity_envelope=capacity_envelope,capacity_profile_snapshot=capacity_profile_snapshot,overrides=overrides,scheduling_policy_provenance=scheduling_policy_provenance,capacity_scope=capacity_scope)
def schedule_legacy_v1(candidates,active_allocations,resource_snapshot,*,capacity_scope=None,**kw):
    """Schedule legacy candidates using the same capacity-scope semantics as :func:`schedule`."""
    flags=[isinstance(x,Mapping) and {"target_id","requested_processors","requested_memory_bytes"}.issubset(x) for x in candidates]
    if any(flags) and not all(flags):raise SchedulingError("legacy candidate cannot be mixed with prepared candidates")
    return _run(candidates,active_allocations,resource_snapshot,legacy=True,capacity_scope=capacity_scope,**kw)
def validate_scheduling_decision(v):
    if not isinstance(v,Mapping):raise SchedulingError("SchedulingDecision must be an object")
    d=_copy(v);did=_text(d.pop("decision_id",None),"decision_id");sv=d.get("schema_version");base={"schema_version","decision_kind","snapshot_revision","resource_snapshot_revision","action","selected_attempt_id","selected_target_id","allocation","reason_code","considered"};
    if sv==2:base|={"option_policy","selected_execution_option_set_id","selected_execution_option","selected_performance_profile_snapshot_id","selected_performance_profile"}
    adaptive="capacity_analysis" in d
    if adaptive:base.add("capacity_analysis")
    if "scheduling_policy_provenance" in d:base.add("scheduling_policy_provenance")
    if set(d)!=base or sv not in {1,2} or d.get("decision_kind")!="scheduling-decision" or d.get("action") not in {"launch","wait"} or not _REVISION.fullmatch(str(d.get("snapshot_revision",""))) or not _REVISION.fullmatch(str(d.get("resource_snapshot_revision",""))) or not isinstance(d.get("considered"),list) or did!=_cid("scheduling-decision",d):raise SchedulingError("SchedulingDecision is invalid")
    if sv==2:_policyopt=_OPTION_POLICIES
    if adaptive and "scheduling_policy_provenance" not in d:raise SchedulingError("adaptive scheduling decisions require policy provenance")
    if "scheduling_policy_provenance" in d:_provenance(d["scheduling_policy_provenance"])
    if d["action"]=="launch":
        a=d["allocation"]
        if not isinstance(a,dict) or set(a)!={"processors","memory_bytes"}:raise SchedulingError("launch SchedulingDecision is invalid")
    elif any(d.get(k) is not None for k in ("selected_attempt_id","selected_target_id","allocation")):raise SchedulingError("wait SchedulingDecision cannot allocate resources")
    return {**d,"decision_id":did}
def make_resource_allocation(decision,*,session_ref,run_id,remote_workspace_root,decision_artifact_id,decision_artifact_path):
    d=validate_scheduling_decision(decision)
    if d["action"]!="launch":raise SchedulingError("only a launch decision can create an allocation")
    if not _RUN_ID.fullmatch(str(run_id)) or not _POSIX_PATH.fullmatch(str(remote_workspace_root)):raise SchedulingError("allocation requires a safe run ID and remote workspace")
    b={"schema_version":1,"decision":d,"attempt_id":d["selected_attempt_id"],"session_ref":_text(session_ref,"session_ref"),"run_id":run_id,"target_id":d["selected_target_id"],"processors":d["allocation"]["processors"],"memory_bytes":d["allocation"]["memory_bytes"],"resource_key":str(remote_workspace_root).rstrip("/")+"/"+run_id,"remote_workspace_root":remote_workspace_root,"scheduling_decision_artifact_id":_text(decision_artifact_id,"scheduling_decision_artifact_id"),"scheduling_decision_path":_text(decision_artifact_path,"scheduling_decision_path")};return {**b,"allocation_id":_cid("resource-allocation",b)}
def validate_resource_allocation(v):
    if not isinstance(v,Mapping):raise SchedulingError("ResourceAllocation must be an object")
    a=_copy(v);aid=_text(a.pop("allocation_id",None),"allocation_id");d=validate_scheduling_decision(a.get("decision",{}));
    if a.get("schema_version")!=1 or d["action"]!="launch" or aid!=_cid("resource-allocation",a):raise SchedulingError("ResourceAllocation is invalid")
    return {**a,"allocation_id":aid}
