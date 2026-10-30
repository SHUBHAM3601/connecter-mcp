"""Automation: rules, change events, background jobs and the library guardian."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from . import jobs as jobqueue
from .client import ConnecterError
from .config import HOME_DIR, Settings
from .files import MODEL_EXTS
from .maintenance import ingest_queue
from .server import _db, audit_library, batch_relocate, ingest_model, search_assets, tag_assets, tool

RULES = HOME_DIR / "rules.json"
EVENTS_STATE = HOME_DIR / "events_state.json"
GUARDIAN_DIR = HOME_DIR / "guardian"


# ---------------------------------------------------------------- rules

def _load_rules() -> list[dict]:
    try:
        return json.loads(RULES.read_text("utf-8"))
    except (OSError, ValueError):
        return []


def _save_rules(rules: list[dict]) -> None:
    RULES.parent.mkdir(parents=True, exist_ok=True)
    RULES.write_text(json.dumps(rules, indent=1), "utf-8")


def _rule_plan(rule: dict) -> list[dict]:
    when, then = rule.get("when", {}), rule.get("then", {})
    pat = re.compile(when.get("name_matches", ".*"), re.I)
    if rule["kind"] == "ingest":
        items = ingest_queue(when.get("folder", "")).get("pending", [])
        return [{"folder": i["folder"], "category": then["category"], "tag": then.get("tag")}
                for i in items if "folder" in i and pat.search(Path(i["folder"]).name)]
    assets = search_assets(folder=when.get("folder", ""), tag=when.get("tag", ""), untagged=when.get("untagged", False),
                           extension=when.get("extension", ".max"), limit=1000000)["assets"]
    return [{"asset": a["path"], "tag": then["tag"]} for a in assets
            if pat.search(a["path"]) and then["tag"] not in a["tags"]]


@tool
def rules(action: str = "list", rule: dict | None = None, rule_id: str = "", dry_run: bool = True) -> dict:
    """Standing rules that file and tag things automatically.
    action='add' with rule = {"name": ..., "kind": "ingest", "when": {"name_matches": "(?i)sofa"},
      "then": {"category": "02_FURNITURES/SOFAS", "tag": "02_FURNITURES/SOFAS"}}  -> downloads whose folder
      name matches go into that library category;
    or {"kind": "tag", "when": {"name_matches": "(?i)pendant", "folder": "...", "untagged": true},
      "then": {"tag": "06_LIGHTS/PENDANT"}}  -> catalogued models whose path matches get the tag.
    action='list', 'remove' (rule_id), or 'run' (all rules, or rule_id). run defaults to dry_run=True.
    The guardian can run rules on a schedule."""
    rs = _load_rules()
    if action == "list":
        return {"rules": rs}
    if action == "add":
        if not rule or rule.get("kind") not in ("ingest", "tag"):
            raise ConnecterError("rule needs kind 'ingest' or 'tag'")
        if rule["kind"] == "ingest" and not rule.get("then", {}).get("category"):
            raise ConnecterError("an ingest rule needs then.category")
        if rule["kind"] == "tag" and not rule.get("then", {}).get("tag"):
            raise ConnecterError("a tag rule needs then.tag")
        re.compile(rule.get("when", {}).get("name_matches", ".*"))
        rule = {"id": uuid.uuid4().hex[:6], "enabled": True, **rule}
        rs.append(rule)
        _save_rules(rs)
        return {"added": rule, "would_affect_now": len(_rule_plan(rule))}
    if action == "remove":
        keep = [r for r in rs if r["id"] != rule_id]
        if len(keep) == len(rs):
            raise ConnecterError(f"No rule {rule_id}")
        _save_rules(keep)
        return {"removed": rule_id}
    if action == "run":
        return run_rules(rule_id, dry_run)
    raise ConnecterError("action must be list, add, remove or run")


def run_rules(rule_id: str = "", dry_run: bool = True) -> dict:
    out = []
    for r in _load_rules():
        if not r.get("enabled", True) or (rule_id and r["id"] != rule_id):
            continue
        plan = _rule_plan(r)
        entry = {"rule": r["id"], "name": r.get("name"), "matches": len(plan), "sample": plan[:5]}
        if not dry_run and plan:
            if r["kind"] == "ingest":
                entry["done"] = [ingest_model(p["folder"], p["category"], tag=p.get("tag"), dry_run=False)["to"] for p in plan]
            else:
                entry["done"] = tag_assets([p["asset"] for p in plan], r["then"]["tag"])
        out.append(entry)
    return {"dry_run": dry_run, "rules": out}


# ---------------------------------------------------------------- events

def _snapshot() -> dict:
    s = Settings.load()
    files = {}
    for root in [p for p in (s.models_dir, s.downloads_dir) if p and p.exists()]:
        for d, _dirs, names in os.walk(root):
            for n in names:
                if Path(n).suffix.lower() in MODEL_EXTS:
                    p = os.path.join(d, n)
                    try:
                        files[p] = os.path.getmtime(p)
                    except OSError:
                        pass
    with _db() as db:
        assets = {a.path: {"tags": a.tags, "previews": a.previews} for a in db.assets()}
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files, "assets": assets}


@tool
def events(reset: bool = False, limit: int = 100) -> dict:
    """What changed since the last call: model files added, removed or modified in the library and
    downloads folders, and assets newly catalogued, removed, re-tagged or given previews in Connecter.
    The first call (or reset=True) just records the current state."""
    now = _snapshot()
    prev = None if reset or not EVENTS_STATE.exists() else json.loads(EVENTS_STATE.read_text("utf-8"))
    EVENTS_STATE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_STATE.write_text(json.dumps(now), "utf-8")
    if not prev:
        return {"baseline_recorded": now["time"], "files": len(now["files"]), "assets": len(now["assets"])}
    f0, f1, a0, a1 = prev["files"], now["files"], prev["assets"], now["assets"]
    retagged = [p for p in a1 if p in a0 and a0[p]["tags"] != a1[p]["tags"]]
    changes = {
        "files_added": [p for p in f1 if p not in f0],
        "files_removed": [p for p in f0 if p not in f1],
        "files_modified": [p for p in f1 if p in f0 and f1[p] != f0[p]],
        "assets_catalogued": [p for p in a1 if p not in a0],
        "assets_removed": [p for p in a0 if p not in a1],
        "assets_retagged": [{"asset": p, "from": a0[p]["tags"], "to": a1[p]["tags"]} for p in retagged],
        "previews_added": [p for p in a1 if p in a0 and a1[p]["previews"] > a0[p]["previews"]],
    }
    return {"since": prev["time"], "until": now["time"], "counts": {k: len(v) for k, v in changes.items()},
            **{k: v[:limit] for k, v in changes.items()}}


# ---------------------------------------------------------------- jobs

@tool
def jobs(action: str = "list", job_id: str = "", include_result: bool = True) -> dict:
    """Background jobs started by long tools (build_index, the guardian): action='list' shows running and
    recent jobs, 'status' one job with its progress and result, 'cancel' stops one, 'history' shows
    finished jobs from earlier sessions."""
    if action == "list":
        return {"jobs": [{k: v for k, v in j.to_dict().items() if k != "result"} for j in jobqueue.all_jobs()]}
    if action == "history":
        return {"jobs": jobqueue.history()}
    job = jobqueue.get(job_id)
    if not job:
        raise ConnecterError(f"No job {job_id} in this session")
    if action == "status":
        d = job.to_dict()
        if not include_result:
            d.pop("result")
        return d
    if action == "cancel":
        return {"cancelled": jobqueue.cancel(job_id), "status": job.status}
    raise ConnecterError("action must be list, status, cancel or history")


# ---------------------------------------------------------------- guardian

_guardian = {"thread": None, "stop": threading.Event(), "interval": 0, "auto_fix": False, "last": None}


def _check(auto_fix: bool) -> dict:
    audit = audit_library()
    relocate = batch_relocate(min_confidence=0.95, dry_run=not auto_fix)
    rule_run = run_rules(dry_run=not auto_fix)
    queue = ingest_queue().get("pending", [])
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "auto_fix": auto_fix,
        "catalogued": audit["catalogued"], "missing_files": audit["missing_files_count"],
        "untagged_models": audit["untagged_models_count"], "models_without_previews": audit["models_without_previews_count"],
        "orphan_previews": audit.get("orphan_preview_images_count"), "downloads_waiting": len(queue),
        "relocatable": relocate.get("relocated") if auto_fix else len(relocate.get("would_relocate", [])),
        "rules": [{"rule": r["rule"], "matches": r["matches"]} for r in rule_run["rules"]],
    }
    prev = _guardian["last"] or _latest_report()
    if prev:
        report["changes_since_last"] = {k: report[k] - prev.get(k, 0) for k in ("catalogued", "missing_files", "untagged_models",
                                                                                "models_without_previews", "downloads_waiting")
                                        if isinstance(prev.get(k), int) and report[k] != prev.get(k)}
    GUARDIAN_DIR.mkdir(parents=True, exist_ok=True)
    (GUARDIAN_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(report, indent=1), "utf-8")
    _guardian["last"] = report
    return report


def _latest_report() -> dict | None:
    files = sorted(GUARDIAN_DIR.glob("*.json")) if GUARDIAN_DIR.exists() else []
    return json.loads(files[-1].read_text("utf-8")) if files else None


def _loop(job, interval: int, auto_fix: bool) -> dict:
    runs = 0
    while not job.cancelled and not _guardian["stop"].is_set():
        try:
            r = _check(auto_fix)
            runs += 1
            job.update(message=f"run {runs} at {r['time']}: {r['missing_files']} missing, {r['untagged_models']} untagged")
        except Exception as e:  # noqa: BLE001 - keep guarding; Connecter may be closed for a while
            job.update(message=f"check failed: {e}")
        _guardian["stop"].wait(interval * 60)
    return {"runs": runs}


@tool
def guardian(action: str = "run", interval_minutes: int = 60, auto_fix: bool = False) -> dict:
    """Keeps an eye on the library: missing files, untagged models, models without previews, orphan
    previews, downloads waiting and what the rules would do, with the change since the last check.
    action='run' checks once; 'start' repeats it every interval_minutes while this MCP server runs;
    'stop'; 'status' shows the last report. auto_fix=True also relocates moved assets (>= 95% sure) and
    applies the rules. Reports are saved in ~/.connecter-mcp/guardian."""
    if action == "run":
        return _check(auto_fix)
    if action == "start":
        if _guardian["thread"] and _guardian["thread"].status == "running":
            return {"guardian": "already running", "job_id": _guardian["thread"].id}
        _guardian["stop"].clear()
        job = jobqueue.submit("guardian", _loop, max(5, interval_minutes), auto_fix,
                              details={"interval_minutes": interval_minutes, "auto_fix": auto_fix})
        _guardian["thread"] = job
        return {"guardian": "started", "job_id": job.id, "interval_minutes": max(5, interval_minutes), "auto_fix": auto_fix}
    if action == "stop":
        _guardian["stop"].set()
        return {"guardian": "stopping"}
    if action == "status":
        job = _guardian["thread"]
        return {"running": bool(job and job.status == "running"), "job": job.to_dict() if job else None,
                "last_report": _guardian["last"] or _latest_report()}
    raise ConnecterError("action must be run, start, stop or status")
