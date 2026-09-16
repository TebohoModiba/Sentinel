"""
Campus Sentinel – Agentic campus crisis report processor (Gemini version).
Compatible with tools.py (real backend with correlation engine).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError

from tools import (
    get_current_incidents,
    find_similar_incidents,
    create_incident,
    update_incident,
    assess_severity_and_confidence,
    select_services_and_actions,
    request_human_review,
    record_decision,
    mark_resolved_or_controlled,
)

# Load environment variables
load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY environment variable is missing.")

client = genai.Client(api_key=api_key)

# Fallback is flash-lite (500 RPD free tier) instead of flash (20 RPD free tier).
# Override by setting AGENT_MODEL in .env.
MODEL_NAME = os.getenv("AGENT_MODEL", "gemini-3.1-flash-lite")
MAX_STEPS = 10

# Gemini function declarations — matched to the real tools.py signatures
TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_current_incidents",
        description=(
            "Return a LIST of compact incident summaries. Each item has: "
            "incident_id, type, location, severity, confidence, status, "
            "report_ids, evidence_count, conflict_flags, needs_human_review, "
            "review_reasons, last_description, action_history."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="find_similar_incidents",
        description=(
            "Find existing incidents whose similarity to this report is >= threshold. "
            "Returns a LIST of matches, best first. Each match has: incident_id, type, "
            "location, severity, status, score, match_reason. Empty list means no match."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "report": types.Schema(
                    type="OBJECT",
                    description="The raw report dict (report_id, category, location, description, timestamp, reporter_type).",
                ),
                "threshold": types.Schema(
                    type="NUMBER",
                    description="Minimum similarity score (0-1). Default 0.40.",
                ),
            },
            required=["report"],
        ),
    ),
    types.FunctionDeclaration(
        name="create_incident",
        description=(
            "Start a new incident from a report. WARNING: if the report matches an "
            "existing incident at >=0.85 similarity, this will AUTO-MERGE into that "
            "incident and return its summary with a '_note' field. Severity can only "
            "escalate (rules set a floor). Returns the incident summary dict."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "report": types.Schema(type="OBJECT"),
                "severity": types.Schema(
                    type="STRING",
                    enum=["low", "medium", "high", "critical"],
                ),
                "confidence": types.Schema(type="NUMBER"),
                "initial_status": types.Schema(
                    type="STRING",
                    enum=["investigating", "active", "escalated"],
                ),
            },
            required=["report", "severity", "confidence"],
        ),
    ),
    types.FunctionDeclaration(
        name="update_incident",
        description=(
            "Merge a report into an existing incident. Detects conflicts, reopens "
            "closed incidents on new ongoing evidence, and may refuse closure while "
            "conflicts are open. Returns the updated incident summary dict."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "incident_id": types.Schema(type="STRING"),
                "report": types.Schema(type="OBJECT"),
                "relationship": types.Schema(
                    type="STRING",
                    enum=[
                        "update", "corroboration", "conflict",
                        "duplicate", "resolution",
                    ],
                ),
                "new_severity": types.Schema(
                    type="STRING",
                    enum=["low", "medium", "high", "critical"],
                ),
                "new_confidence": types.Schema(type="NUMBER"),
                "new_status": types.Schema(
                    type="STRING",
                    enum=[
                        "investigating", "active", "escalated",
                        "controlled", "resolved", "reopened",
                    ],
                ),
            },
            required=["incident_id", "report", "relationship"],
        ),
    ),
    types.FunctionDeclaration(
        name="assess_severity_and_confidence",
        description=(
            "Read-only recompute of severity/confidence for an incident. Does NOT "
            "mutate state. Returns: incident_id, severity, confidence, evidence_count, "
            "conflict_count, last_signal, rationale."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "incident_id": types.Schema(type="STRING"),
                "report": types.Schema(type="OBJECT"),
            },
            required=["incident_id", "report"],
        ),
    ),
    types.FunctionDeclaration(
        name="select_services_and_actions",
        description=(
            "Choose services and actions for an incident. Returns "
            "{'actions': [...], 'reasoning': '...'}. An EMPTY actions list means the "
            "existing response is already sufficient — this is the correct answer for "
            "a duplicate at the same severity tier. Each action has: type, service_id, "
            "service_name."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "incident_id": types.Schema(type="STRING"),
                "report": types.Schema(type="OBJECT"),
                "current_severity": types.Schema(type="STRING"),
                "current_status": types.Schema(type="STRING"),
            },
            required=["incident_id", "report"],
        ),
    ),
    types.FunctionDeclaration(
        name="request_human_review",
        description=(
            "Flag an incident for human review. Returns incident_id, "
            "needs_human_review, review_reasons."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "incident_id": types.Schema(type="STRING"),
                "reason": types.Schema(type="STRING"),
            },
            required=["incident_id", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name="record_decision",
        description=(
            "Persist the final structured decision for this report. Returns the "
            "recorded decision dict with keys: report_id, incident_id, relationship "
            "(canonical: new/related/conflicting/duplicate/resolution), severity "
            "(UPPERCASE), confidence, decision, service, status, reason, "
            "incident_status, human_review, actions. ALWAYS include a concise "
            "'reason' string."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "report_id": types.Schema(type="STRING"),
                "incident_id": types.Schema(type="STRING"),
                "relationship": types.Schema(
                    type="STRING",
                    description="One of: new, update, corroboration, conflict, duplicate, resolution, related, conflicting.",
                ),
                "severity": types.Schema(
                    type="STRING",
                    enum=["low", "medium", "high", "critical", "LOW", "MEDIUM", "HIGH", "CRITICAL"],
                ),
                "confidence": types.Schema(type="NUMBER"),
                "actions": types.Schema(
                    type="ARRAY",
                    items=types.Schema(type="OBJECT"),
                    description="List of action dicts (type, service_id, service_name). May be empty.",
                ),
                "incident_status": types.Schema(type="STRING"),
                "human_review": types.Schema(type="BOOLEAN"),
                "reason": types.Schema(
                    type="STRING",
                    description="Short human-readable explanation of the decision.",
                ),
            },
            required=[
                "report_id", "incident_id", "relationship", "severity",
                "confidence", "actions", "incident_status", "human_review", "reason",
            ],
        ),
    ),
    types.FunctionDeclaration(
        name="mark_resolved_or_controlled",
        description=(
            "Explicitly close an incident as RESOLVED or CONTROLLED. Refuses if "
            "conflict flags are outstanding. Returns incident_id, status, refused."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "incident_id": types.Schema(type="STRING"),
                "status": types.Schema(
                    type="STRING",
                    enum=["resolved", "controlled"],
                ),
                "reason": types.Schema(type="STRING"),
            },
            required=["incident_id", "status"],
        ),
    ),
]

TOOL_MAP = {
    "get_current_incidents": get_current_incidents,
    "find_similar_incidents": find_similar_incidents,
    "create_incident": create_incident,
    "update_incident": update_incident,
    "assess_severity_and_confidence": assess_severity_and_confidence,
    "select_services_and_actions": select_services_and_actions,
    "request_human_review": request_human_review,
    "record_decision": record_decision,
    "mark_resolved_or_controlled": mark_resolved_or_controlled,
}

SYSTEM_PROMPT = """
You are Campus Sentinel. Process campus reports in arrival order.

A report is evidence, not automatically a new incident.
Follow: Observe -> Correlate -> Assess -> Decide -> Act -> Record -> Monitor -> Reassess.

Workflow for each report:
1. Call get_current_incidents to see what's already open.
2. Call find_similar_incidents(report=...) to check for a match.
3. If a match exists at high score, call update_incident with the correct
   relationship (update / corroboration / conflict / duplicate / resolution).
   If no match, call create_incident.
4. Call select_services_and_actions. An EMPTY actions list is the correct
   answer when the existing response already covers this severity tier.
5. If uncertainty or consequences are high, call request_human_review.
6. Call record_decision with a concise "reason" string and STOP.

Relationship vocabulary (use these exact values):
  NEW -> "new"
  UPDATE / CORROBORATION / RESOLUTION -> "related"
  CONFLICT -> "conflicting"
  DUPLICATE -> "duplicate"

Prefer CONTINUE_RESPONSE or empty actions over unnecessary dispatches.
Request human_review on high uncertainty or severe consequences.

Be efficient: don't call tools you don't need. Aim to reach record_decision
in as few steps as possible.

Use tools only. Call record_decision when ready and stop.
"""


def _safe_json_loads(text: str, default: Any = None) -> Any:
    if not text or not isinstance(text, str):
        return default
    text = text.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def run_agent_on_report(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = types.Tool(function_declarations=TOOL_DECLARATIONS)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[tools],
        temperature=0.1,
    )

    contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=(
                "Process this report. Use tools as needed.\n\n"
                f"REPORT:\n{json.dumps(report, indent=2, default=str)}"
            ))],
        )
    ]

    log: List[Dict[str, Any]] = []
    step = 0
    retries = 0
    max_retries = 3

    while step < MAX_STEPS:
        step += 1
        # Small pause between turns to reduce RPM pressure.
        time.sleep(1)

        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config,
            )
            retries = 0
        except APIError as e:
            err_code = getattr(e, "code", None)
            err_str = str(e)
            is_transient = (
                err_code in (429, 503)
                or "429" in err_str
                or "503" in err_str
                or "UNAVAILABLE" in err_str
                or "RESOURCE_EXHAUSTED" in err_str
            )

            if is_transient and retries < max_retries:
                retries += 1
                delay_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                if delay_match:
                    wait_time = float(delay_match.group(1)) + 1.0
                else:
                    wait_time = retries * 5.0
                print(f"\n[Server Busy/Limit Exceeded] Retrying step {step} in {wait_time:.1f}s (Attempt {retries}/{max_retries})...")
                time.sleep(wait_time)
                step -= 1
                continue

            log.append({
                "step": step,
                "thought": "LLM call failed",
                "action": None,
                "observation": f"LLM ERROR: {type(e).__name__}: {e}",
            })
            break
        except Exception as e:
            log.append({
                "step": step,
                "thought": "LLM call failed",
                "action": None,
                "observation": f"LLM ERROR: {type(e).__name__}: {e}",
            })
            break

        # Collect thought text
        thought_texts = []
        try:
            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts or []:
                    if part.text:
                        thought_texts.append(part.text.strip())
        except Exception:
            pass

        thought = "\n".join(thought_texts).strip() if thought_texts else "(no explicit thought)"

        # Collect function calls
        function_calls = []
        try:
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)
        except Exception:
            pass

        if not function_calls:
            log.append({
                "step": step,
                "thought": thought,
                "action": None,
                "observation": "Agent finished without further tool calls.",
            })
            break

        function_response_parts = []
        recorded = False
        for fc in function_calls:
            fn_name = fc.name
            args = dict(fc.args) if fc.args else {}

            entry = {
                "step": step,
                "thought": thought,
                "action": fn_name,
                "action_input": args,
            }

            if fn_name not in TOOL_MAP:
                observation = f"ERROR: unknown tool '{fn_name}'"
            else:
                try:
                    result = TOOL_MAP[fn_name](**args)
                    if isinstance(result, str):
                        observation = result
                    else:
                        try:
                            observation = json.dumps(result, default=str)
                        except (TypeError, ValueError):
                            observation = str(result)
                except Exception as e:
                    observation = f"TOOL ERROR: {type(e).__name__}: {e}"

            entry["observation"] = observation
            log.append(entry)

            function_response_parts.append(
                types.Part.from_function_response(
                    name=fn_name,
                    response={"result": observation},
                )
            )

            if fn_name == "record_decision":
                pred = _safe_json_loads(observation, default=None)
                if pred is None or not isinstance(pred, dict):
                    pred = args if isinstance(args, dict) else {}
                log.append({"step": step, "final_prediction": pred})
                recorded = True

        if recorded:
            return log

        contents.append(response.candidates[0].content)
        contents.append(types.Content(role="user", parts=function_response_parts))

    return log


def process_report(report: Dict[str, Any]) -> Dict[str, Any]:
    reasoning_log = run_agent_on_report(report)
    prediction = None
    for entry in reversed(reasoning_log):
        if "final_prediction" in entry:
            prediction = entry["final_prediction"]
            break
    return {
        "report_id": report.get("report_id"),
        "reasoning_log": reasoning_log,
        "prediction": prediction,
    }


if __name__ == "__main__":
    sample = {
        "report_id": "R018",
        "timestamp": "2026-09-16T09:55",
        "location": "Engineering Block E3",
        "category": "fire",
        "reported_severity": "CRITICAL",
        "description": "A second lecturer confirms smoke and asks students to leave.",
        "reporter_type": "staff",
    }
    result = process_report(sample)

    # Save decision to predictions.jsonl for dashboard.py
    if result.get("prediction"):
        with open("predictions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(result["prediction"], default=str) + "\n")

    print(json.dumps(result, indent=2, default=str))