

from __future__ import annotations
import csv
import json
import os
import uuid
from typing import Any, Optional

import utils


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
TIME_WINDOW_MINUTES     = 240
STRONG_MATCH            = 0.62   # >= this -> same incident
WEAK_MATCH              = 0.40   # >= this -> related but uncertain
AUTO_MERGE_OVERRIDE     = 0.85   # create_incident() redirects above this
                                    # so LLM drift can't spawn duplicates

SAFETY_CRITICAL = {
    "fire_smoke_electrical", "security_violence",
    "structural_hazard", "medical",
}

_SEV_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_ACTION_BY_SEV = {
    "critical": "EMERGENCY_DISPATCH",
    "high":     "PRIORITY_DISPATCH",
    "medium":   "STANDARD_DISPATCH",
    "low":      "LOG_ONLY_NOTIFY",
}

# service_type values in campus_services.csv -> canonical incident categories
_SERVICE_TYPE_MAP = {
    "security":       "security_violence",
    "medical":        "medical",
    "fire":           "fire_smoke_electrical",
    "electrical":     "fire_smoke_electrical",
    "facilities":     "structural_hazard",
    "it":             "network_outage",
    "accessibility":  "lift_accessibility",
    "environmental":  "other",
    "coordination":   "other",
    "communications": "other",
    "wellness":       "other",
}

# LLM relationship vocabulary -> brief vocabulary
_REL_MAP = {
    "new":           "new",
    "update":        "related",
    "corroboration": "related",
    "related":       "related",
    "conflict":      "conflicting",
    "conflicting":   "conflicting",
    "duplicate":     "duplicate",
    "resolution":    "resolution",
}

# Raw category values that name a DEPARTMENT, not an incident type.
# "facilities" could be a leak, a handrail, or broken glass — three unrelated
# incidents. Let the description disambiguate.
_DEPARTMENT_CATEGORIES = {
    "facilities", "cleaning", "environmental",
    "maintenance", "general", "it", "electrical",
}


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_INCIDENTS: dict[str, dict] = {}
_SERVICES:  dict[str, list] = {}
_DECISIONS: list[dict] = []


def reset_state() -> None:
    """Clear incidents + decisions. Services are cached across calls."""
    global _INCIDENTS, _DECISIONS
    _INCIDENTS = {}
    _DECISIONS = []


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_reports(csv_path: str) -> list[dict]:
    """
    Kept for run_agent.py / smoke tests. agent.py does its own loading — this
    just gives you the same normalized shape for local runs.
    """
    out = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if not row:
                continue
            out.append(_coerce_raw_row(row, i))
    return out


def load_services(csv_path: str) -> dict[str, list]:
    """Read campus_services.csv into {canonical_category: [service, ...]}."""
    global _SERVICES
    services: dict[str, list] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = utils.safe_get(row, "service_name", "name", default="")
            if not name:
                continue
            raw_type = str(utils.safe_get(row, "service_type", "type",
                                          "category", default="")).strip().lower()
            bucket = _SERVICE_TYPE_MAP.get(raw_type, "other")
            services.setdefault(bucket, []).append({
                "service_id":   utils.safe_get(row, "service_id", default=""),
                "name":         str(name),
                "availability": utils.safe_get(row, "availability", default=""),
            })
    _SERVICES = services
    return services


def _ensure_services() -> None:
    """Lazy-load services if run_agent.py forgot to. Tries common filenames."""
    if _SERVICES:
        return
    for candidate in ("campus_services.csv", "services.csv",
                       os.path.join(os.path.dirname(__file__), "campus_services.csv")):
        if os.path.exists(candidate):
            try:
                load_services(candidate)
                return
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Report normalization
# ---------------------------------------------------------------------------
def _coerce_raw_row(row: dict, i: int = 0) -> dict:
    """Normalize one raw CSV row into the shape every tool function expects."""
    raw_cat  = str(utils.safe_get(row, "category", "type", "incident_type",
                                   default="")).strip()
    raw_desc = str(utils.safe_get(row, "description", "details", "notes",
                                   "text", default=""))

    category = utils.normalize_category(raw_cat)
    # Department-style category OR unclassified -> retry against description.
    # This is what stops "facilities" from dumping leaks, handrails and
    # glass into one bucket.
    if raw_cat.lower() in _DEPARTMENT_CATEGORIES or category == "other":
        better = utils.normalize_category(f"{raw_cat} {raw_desc[:200]}")
        if better != "other":
            category = better

    ts_raw = utils.safe_get(row, "timestamp", "time", "reported_at",
                             "datetime", default=None)

    return {
        "report_id":     str(utils.safe_get(row, "report_id", "id",
                                             default=f"R{i+1:04d}")).strip(),
        "timestamp_raw": ts_raw,
        "timestamp":     utils.parse_timestamp(ts_raw),
        "category_raw":  raw_cat,
        "category":      category,
        "location_raw":  utils.safe_get(row, "location", "site", "building",
                                         default=""),
        "location":      utils.normalize_location(
                             utils.safe_get(row, "location", "site",
                                            "building", default="")),
        "description":   raw_desc,
        "reporter":      utils.safe_get(row, "reporter_type", "reporter",
                                         default="unknown"),
        "sequence_index": i,
    }


def _coerce_report(report: Any) -> Optional[dict]:
    """
    Accept whatever the LLM passes as `report`:
      - a dict straight from agent.py's user message (raw CSV shape)
      - a JSON string of the above
      - None / garbage -> None
    Always returns the canonical normalized shape or None.
    """
    if report is None:
        return None
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except Exception:
            return None
    if not isinstance(report, dict):
        return None

    # Already-normalized shape? Re-coerce from the raw fields we know exist.
    if "category" in report and "location" in report and "report_id" in report:
        # Could be either — route through _coerce_raw_row either way.
        return _coerce_raw_row(report, report.get("sequence_index", 0))

    return _coerce_raw_row(report, 0)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------
def _score(incident: dict, report: dict) -> tuple[float, str]:
    """Return (score 0-1, short reason). Gates return (0.0, reason)."""
    if incident["type"] != report["category"]:
        return 0.0, f"category differs ({incident['type']} vs {report['category']})"

    loc_s = utils.location_similarity(incident["location"],
                                       report["location"]) / 100.0
    txt_s = 0.0
    if incident.get("last_description"):
        txt_s = utils.text_similarity(incident["last_description"],
                                       report["description"]) / 100.0

    gap = utils.minutes_between(incident.get("last_timestamp"),
                                 report.get("timestamp"))
    if gap is None:
        time_s = 0.5
    elif gap <= TIME_WINDOW_MINUTES:
        time_s = 1.0
    else:
        time_s = max(0.0, 1.0 - (gap - TIME_WINDOW_MINUTES)
                                 / (TIME_WINDOW_MINUTES * 4))

    both_known = (incident["location"] != "unknown"
                  and report["location"] != "unknown")
    if both_known and loc_s < 0.30 and txt_s < 0.75:
        return 0.0, "locations differ and text isn't a near-duplicate"

    score = 0.55 * loc_s + 0.20 * time_s + 0.25 * txt_s
    return score, f"loc={loc_s:.2f} time={time_s:.2f} text={txt_s:.2f}"


def find_similar_incidents(report: Any = None,
                            threshold: float = WEAK_MATCH,
                            **_: Any) -> list:
    """
    All existing incidents whose score >= threshold, best first. Returns a
    list (possibly empty). The LLM can call this with no args and get [].
    """
    rep = _coerce_report(report)
    if rep is None:
        return []
    matches = []
    for inc_id, inc in _INCIDENTS.items():
        s, why = _score(inc, rep)
        if s >= threshold:
            matches.append({
                "incident_id": inc_id,
                "type":        inc["type"],
                "location":    inc["location"],
                "severity":    inc["severity"].upper(),
                "status":      inc["status"].upper(),
                "score":       round(s, 3),
                "match_reason": why,
            })
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def create_incident(report: Any = None,
                     severity: str = "MEDIUM",
                     confidence: float = 0.5,
                     initial_status: Optional[str] = None,
                     **_: Any) -> dict:
    """
    Start a new incident. Two safety rails the LLM doesn't have to opt into:
      - If the report matches an existing incident at AUTO_MERGE_OVERRIDE or
        above, redirect into that incident instead of creating a duplicate.
        LLMs routinely skip find_similar_incidents and this is what keeps
        correlation scoring intact.
      - Rule severity sets a floor; the LLM's severity can only escalate.
    """
    rep = _coerce_report(report)
    if rep is None:
        return {"error": "no report supplied"}

    # Auto-merge override — protects against LLM drift.
    for inc_id, inc in _INCIDENTS.items():
        s, _ = _score(inc, rep)
        if s >= AUTO_MERGE_OVERRIDE:
            updated = update_incident(inc_id, rep, "corroboration")
            updated["_note"] = (
                f"redirected from create_incident: matched {inc_id} at {s:.2f}"
            )
            return updated

    inc_id = f"INC-{uuid.uuid4().hex[:8]}"
    requested = str(severity).lower() if severity else "medium"
    status = (str(initial_status).lower() if initial_status else "investigating")

    incident = {
        "incident_id":       inc_id,
        "type":              rep["category"],
        "location":          rep["location"],
        "severity":          requested,
        "confidence":        float(confidence) if confidence is not None else 0.5,
        "status":            status,
        "report_ids":        [rep["report_id"]],
        "evidence_count":    1,
        "services":          [],
        "action_history":    [],
        "conflict_flags":    [],
        "last_description":  rep["description"],
        "last_timestamp":    rep.get("timestamp"),
        "first_timestamp":   rep.get("timestamp"),
        "last_signal":       None,
        "signal_history":    [],
        "needs_human_review": False,
        "review_reasons":    [],
    }

    sig = utils.detect_status_signal(rep["description"])
    if sig == "conflict":
        incident["conflict_flags"].append(
            f"{rep['report_id']}: report asserts both resolved and ongoing"
        )
    elif sig in ("resolved", "ongoing"):
        incident["last_signal"] = sig
        incident["signal_history"].append((rep["report_id"], sig))

    rule_sev = _compute_severity(incident)
    incident["severity"] = _max_sev(requested, rule_sev)

    if incident["type"] in SAFETY_CRITICAL:
        incident["needs_human_review"] = True
        incident["review_reasons"].append("safety-critical category")

    _INCIDENTS[inc_id] = incident
    return _public_incident(incident)


def update_incident(incident_id: str = "",
                     report: Any = None,
                     relationship: str = "update",
                     new_severity: Optional[str] = None,
                     new_confidence: Optional[float] = None,
                     new_status: Optional[str] = None,
                     **_: Any) -> dict:
    """
    Merge a report into an incident. Detects conflict on evidence flip-flop
    (resolved -> ongoing), NOT on normal progress (stuck -> still stuck ->
    fixed). Respects LLM-escalation-only severity, blocks closure while
    conflict flags are open, and reopens on fresh ongoing evidence.
    """
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return {"error": f"unknown incident_id '{incident_id}'"}

    rep = _coerce_report(report)
    if rep is None:
        return {"error": "no report supplied"}

    incident["report_ids"].append(rep["report_id"])
    incident["evidence_count"] += 1

    sig = utils.detect_status_signal(rep["description"])

    if sig == "conflict":
        incident["conflict_flags"].append(
            f"{rep['report_id']}: report asserts both resolved and ongoing"
        )
    elif sig == "ongoing" and incident.get("last_signal") == "resolved":
        incident["conflict_flags"].append(
            f"{rep['report_id']}: contradicts earlier resolved evidence — "
            f"reported ongoing again"
        )
        incident["last_signal"] = "ongoing"
        incident["signal_history"].append((rep["report_id"], "ongoing"))
    elif sig is not None:
        incident["last_signal"] = sig
        incident["signal_history"].append((rep["report_id"], sig))

    # Location refinement / divergence flag
    loc = rep["location"]
    if loc not in ("unknown", ""):
        sim = utils.location_similarity(incident["location"], loc)
        if sim < 50:
            incident["conflict_flags"].append(
                f"{rep['report_id']}: location '{loc}' diverges from "
                f"'{incident['location']}'"
            )
        else:
            incident["location"] = loc

    ts = rep.get("timestamp")
    if ts is not None:
        if incident["last_timestamp"] is None or ts > incident["last_timestamp"]:
            incident["last_timestamp"] = ts
        if incident["first_timestamp"] is None:
            incident["first_timestamp"] = ts

    incident["last_description"] = rep["description"]

    if new_confidence is not None:
        try:
            incident["confidence"] = round(float(new_confidence), 3)
        except (TypeError, ValueError):
            pass
    else:
        incident["confidence"] = round(max(
            0.1,
            min(0.95, 0.5 + 0.1 * min(incident["evidence_count"], 4)
                      - 0.15 * len(incident["conflict_flags"]))
        ), 3)

    # Severity: rules set the floor, LLM can only escalate.
    rule_sev = _compute_severity(incident)
    incident["severity"] = rule_sev
    if new_severity:
        incident["severity"] = _max_sev(rule_sev, str(new_severity).lower())

    # Status: LLM can propose, but not close while conflicts are open.
    if new_status:
        proposed = str(new_status).lower()
        if proposed in ("resolved", "controlled") and incident["conflict_flags"]:
            incident["review_reasons"].append(
                f"refused {proposed} at {rep['report_id']}: conflict open"
            )
        else:
            incident["status"] = proposed

    if incident["status"] in ("resolved", "controlled") and sig == "ongoing":
        incident["status"] = "reopened"
        incident["review_reasons"].append(
            f"reopened at {rep['report_id']}: new ongoing evidence"
        )

    auto = _auto_review_reason(incident)
    if auto:
        incident["needs_human_review"] = True
        if auto not in incident["review_reasons"]:
            incident["review_reasons"].append(auto)

    return _public_incident(incident)


def assess_severity_and_confidence(incident_id: str = "",
                                    report: Any = None,
                                    **_: Any) -> dict:
    """Read-only recompute. Does NOT mutate state."""
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return {"error": f"unknown incident_id '{incident_id}'"}
    sev = _compute_severity(incident)
    return {
        "incident_id":    incident_id,
        "severity":       sev.upper(),
        "confidence":     incident["confidence"],
        "evidence_count": incident["evidence_count"],
        "conflict_count": len(incident["conflict_flags"]),
        "last_signal":    incident.get("last_signal") or "none",
        "rationale":      (f"type={incident['type']}, "
                           f"evidence={incident['evidence_count']}, "
                           f"conflicts={len(incident['conflict_flags'])}, "
                           f"last_signal={incident.get('last_signal')}"),
    }


def select_services_and_actions(incident_id: str = "",
                                 report: Any = None,
                                 current_severity: Optional[str] = None,
                                 current_status: Optional[str] = None,
                                 **_: Any) -> dict:
    """
    Returns {"actions": [...], "reasoning": "..."}.
    Empty actions list means the existing response is already sufficient —
    the correct answer for a duplicate at the same severity tier.
    """
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return {"actions": [], "reasoning": f"unknown incident_id '{incident_id}'"}

    status = (str(current_status).lower() if current_status
              else incident["status"])
    if status in ("resolved", "controlled"):
        return {"actions": [],
                "reasoning": f"incident {status}, no dispatch needed"}

    severity = (str(current_severity).lower() if current_severity
                else incident["severity"])

    # Duplicate-dispatch guard.
    already = {a.get("severity_at_dispatch")
               for a in incident["action_history"]
               if a.get("outcome") == "sent"}
    if severity in already:
        return {"actions": [],
                "reasoning": f"already dispatched at severity '{severity}'"}

    _ensure_services()
    candidates = _SERVICES.get(incident["type"]) or _SERVICES.get("other") or []
    if not candidates:
        return {"actions": [],
                "reasoning": "no matching service in directory"}

    svc = candidates[0]
    action_type = _ACTION_BY_SEV.get(severity, "STANDARD_DISPATCH")
    actions = [{
        "type":         action_type,
        "service_id":   svc.get("service_id") or svc["name"],
        "service_name": svc["name"],
    }]

    # Lift with trapped occupants -> also page coordination.
    if (incident["type"] == "lift_accessibility"
            and "trapped" in incident["last_description"].lower()):
        for extra in _SERVICES.get("other", []):
            if "duty manager" in extra["name"].lower():
                actions.append({
                    "type":         "COORDINATE",
                    "service_id":   extra.get("service_id", ""),
                    "service_name": extra["name"],
                })
                break

    return {"actions": actions,
            "reasoning": f"{action_type} -> {svc['name']} at severity {severity}"}


def request_human_review(incident_id: str = "",
                          reason: str = "",
                          **_: Any) -> dict:
    """Explicit review flag. Complements the automatic check in update_incident."""
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return {"error": f"unknown incident_id '{incident_id}'"}
    incident["needs_human_review"] = True
    if reason and reason not in incident["review_reasons"]:
        incident["review_reasons"].append(str(reason))
    return {
        "incident_id": incident_id,
        "needs_human_review": True,
        "review_reasons": list(incident["review_reasons"]),
    }


def record_decision(report_id: str = "",
                     incident_id: str = "",
                     relationship: str = "update",
                     severity: str = "MEDIUM",
                     confidence: float = 0.5,
                     actions: Any = None,
                     incident_status: str = "open",
                     human_review: bool = False,
                     reason: str = "",
                     **_: Any) -> dict:
    """
    Build the structured prediction row. This is what agent.py extracts as
    final_prediction and what run_agent.py writes to predictions.jsonl.
    Maps the LLM's relationship vocabulary to the brief's, folds in the
    automatic review flag from the incident, and derives a `decision` label.
    """
    incident = _INCIDENTS.get(incident_id, {})
    canonical_rel = _REL_MAP.get(str(relationship).lower(), "related")
    canonical_status = (str(incident_status).lower() if incident_status
                        else incident.get("status", "open")).lower()

    # Normalize actions — LLM may pass None, a dict, a list of strings, or a
    # list of dicts. Reduce all of those to a list of dicts with both
    # service_id and service_name keys present.
    clean_actions = _normalize_actions(actions)

    # Effective review = what the LLM asked for OR what the rules already flagged.
    effective_review = bool(human_review) or bool(incident.get("needs_human_review"))

    primary_service = ""
    if clean_actions:
        a0 = clean_actions[0]
        primary_service = a0.get("service_id") or a0.get("service_name", "")

    # Deterministic fallback reason if the LLM sent an empty string.
    if not reason or not str(reason).strip():
        reason = _auto_reason(canonical_rel, severity, clean_actions,
                               effective_review, incident)

    record = {
        # Brief-required contract fields
        "report_id":    str(report_id),
        "incident_id":  str(incident_id),
        "relationship": canonical_rel,
        "severity":     str(severity).upper(),
        "confidence":   round(float(confidence), 3) if confidence is not None else 0.0,
        "decision":     _decision_label(canonical_rel, clean_actions, effective_review),
        "service":      primary_service,
        "status":       canonical_status,
        "reason":       str(reason).strip(),

        # Dashboard-compat extras
        "incident_status": canonical_status,
        "human_review":    effective_review,
        "actions":         clean_actions,
    }
    _DECISIONS.append(record)
    return record


def mark_resolved_or_controlled(incident_id: str = "",
                                 status: str = "",
                                 reason: Optional[str] = None,
                                 **_: Any) -> dict:
    """Explicit closure. Refuses while conflicts are open."""
    incident = _INCIDENTS.get(incident_id)
    if incident is None:
        return {"error": f"unknown incident_id '{incident_id}'"}
    target = str(status).lower()
    if target not in ("resolved", "controlled"):
        return {"error": f"status must be resolved or controlled, got '{status}'"}
    if incident["conflict_flags"]:
        return {
            "incident_id": incident_id,
            "status":      incident["status"].upper(),
            "refused":     True,
            "reason":      "conflict flags outstanding — closure suppressed",
        }
    incident["status"] = target
    if reason:
        incident["review_reasons"].append(f"closed: {reason}")
    return {"incident_id": incident_id,
            "status": target.upper(),
            "refused": False}


def get_current_incidents(**_: Any) -> list:
    """Compact summaries the LLM can reason over on the next report."""
    return [_public_incident(inc) for inc in _INCIDENTS.values()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _public_incident(inc: dict) -> dict:
    """JSON-safe summary of an incident for LLM consumption."""
    return {
        "incident_id":     inc["incident_id"],
        "type":            inc["type"],
        "location":        inc["location"],
        "severity":        inc["severity"].upper(),
        "confidence":      inc["confidence"],
        "status":          inc["status"].upper(),
        "report_ids":      list(inc["report_ids"]),
        "evidence_count":  inc["evidence_count"],
        "conflict_flags":  list(inc["conflict_flags"]),
        "needs_human_review": inc.get("needs_human_review", False),
        "review_reasons":  list(inc.get("review_reasons", [])),
        "last_description":inc.get("last_description", ""),
        "action_history":  list(inc.get("action_history", [])),
    }


def _normalize_actions(actions: Any) -> list:
    """Reduce whatever the LLM sent into a list of well-formed action dicts."""
    if actions is None:
        return []
    if isinstance(actions, dict):
        actions = [actions]
    if isinstance(actions, str):
        return [{"type": actions, "service_id": "", "service_name": ""}]
    if not isinstance(actions, list):
        return []

    out = []
    for a in actions:
        if isinstance(a, str):
            out.append({"type": a, "service_id": "", "service_name": ""})
        elif isinstance(a, dict):
            svc_name = (a.get("service_name") or a.get("name")
                         or a.get("service") or "")
            svc_id = a.get("service_id") or svc_name or a.get("service", "")
            out.append({
                "type":         a.get("type") or a.get("action") or "DISPATCH",
                "service_id":   svc_id,
                "service_name": svc_name,
            })
    return out


def _max_sev(a: str, b: str) -> str:
    a, b = (a or "low").lower(), (b or "low").lower()
    return a if _SEV_ORDER.get(a, 0) >= _SEV_ORDER.get(b, 0) else b


def _compute_severity(incident: dict) -> str:
    """Rule severity. Multiple weak reports escalate cumulatively."""
    score = 0
    if incident["type"] in SAFETY_CRITICAL:
        score += 2
    if incident["type"] == "network_outage":
        score += 1

    n = incident["evidence_count"]
    if n >= 5:      score += 3
    elif n >= 3:    score += 2
    elif n >= 2:    score += 1

    if incident["conflict_flags"]:
        score += 1

    if incident.get("last_signal") == "ongoing":
        score += 1
    elif incident.get("last_signal") == "resolved":
        score -= 1

    if score >= 5:  return "critical"
    if score >= 3:  return "high"
    if score >= 1:  return "medium"
    return "low"


def _auto_review_reason(incident: dict) -> Optional[str]:
    """Return a review reason string, or None."""
    if incident["type"] in SAFETY_CRITICAL:
        return "safety-critical category"
    if (incident["severity"] in ("high", "critical")
            and incident["confidence"] < 0.6):
        return "high severity with low confidence"
    if incident["conflict_flags"]:
        return "conflicting evidence"
    return None


def _decision_label(relationship: str, actions: list, review: bool) -> str:
    if actions:
        return "dispatch"
    if relationship == "conflicting":
        return "hold_for_review"
    if relationship == "resolution":
        return "resolve"
    if review:
        return "escalate_to_human"
    return "no_new_action"


def _auto_reason(relationship: str, severity: str, actions: list,
                  review: bool, incident: dict) -> str:
    """Deterministic reason used when the LLM forgets to supply one."""
    bits = [f"{relationship} at severity {severity.upper()}"]
    if actions:
        bits.append("dispatched " + ", ".join(
            a.get("service_name") or a.get("service_id") or "?"
            for a in actions
        ))
    else:
        bits.append("no new dispatch")
    if incident.get("conflict_flags"):
        bits.append(f"{len(incident['conflict_flags'])} conflict flag(s)")
    if review:
        bits.append("flagged for human review")
    return "; ".join(bits) + "."