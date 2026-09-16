"""
Sentinel – Agentic campus crisis report processor.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI

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

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.getenv("AGENT_MODEL", "gpt-4o")
MAX_STEPS = 12

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_incidents",
            "description": "Return currently tracked incidents with state, severity, confidence, reports and action history.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_similar_incidents",
            "description": "Find existing incidents related by location, category, description or time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {"type": "object"},
                    "threshold": {"type": "number", "default": 0.55},
                },
                "required": ["report"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_incident",
            "description": "Start a new incident from the current report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {"type": "object"},
                    "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                    "confidence": {"type": "number"},
                    "initial_status": {"type": "string", "enum": ["INVESTIGATING", "ACTIVE", "ESCALATED"]},
                },
                "required": ["report", "severity", "confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_incident",
            "description": "Link report to an existing incident and update its state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "report": {"type": "object"},
                    "relationship": {
                        "type": "string",
                        "enum": ["UPDATE", "CORROBORATION", "CONFLICT", "DUPLICATE", "RESOLUTION"],
                    },
                    "new_severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                    "new_confidence": {"type": "number"},
                    "new_status": {
                        "type": "string",
                        "enum": ["INVESTIGATING", "ACTIVE", "ESCALATED", "CONTROLLED", "RESOLVED"],
                    },
                },
                "required": ["incident_id", "report", "relationship"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assess_severity_and_confidence",
            "description": "Re-evaluate severity and confidence given new and existing evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "report": {"type": "object"},
                },
                "required": ["incident_id", "report"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_services_and_actions",
            "description": "Choose services and actions. Prefer CONTINUE_RESPONSE or empty actions over repeat dispatches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "report": {"type": "object"},
                    "current_severity": {"type": "string"},
                    "current_status": {"type": "string"},
                },
                "required": ["incident_id", "report"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_review",
            "description": "Flag incident for human review when uncertainty or consequences are high.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["incident_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_decision",
            "description": "Persist the final structured decision for this report. Always include a concise human-readable reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "report_id": {"type": "string"},
                    "incident_id": {"type": "string"},
                    "relationship": {"type": "string"},
                    "severity": {"type": "string"},
                    "confidence": {"type": "number"},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "incident_status": {"type": "string"},
                    "human_review": {"type": "boolean"},
                    "reason": {
                        "type": "string",
                        "description": "Short human-readable explanation of why this decision was made (required for traceability).",
                    },
                },
                "required": [
                    "report_id",
                    "incident_id",
                    "relationship",
                    "severity",
                    "confidence",
                    "actions",
                    "incident_status",
                    "human_review",
                    "reason",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_resolved_or_controlled",
            "description": "Set incident to CONTROLLED or RESOLVED when evidence supports it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["CONTROLLED", "RESOLVED"]},
                    "reason": {"type": "string"},
                },
                "required": ["incident_id", "status"],
            },
        },
    },
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
Follow: Observe → Correlate → Assess → Decide → Act → Record → Monitor → Reassess.

Decide relationship: NEW / UPDATE / CORROBORATION / CONFLICT / DUPLICATE / RESOLUTION.
Prefer CONTINUE_RESPONSE or empty actions over unnecessary dispatches.
Request human_review on high uncertainty or severe consequences.

When you call record_decision you MUST include a concise "reason" string that explains
the decision in plain English (e.g. "Same location + fire keywords as R002/R010, escalating on repeated smoke reports").
Use tools only. Call record_decision when ready and stop.
"""


def _safe_json_loads(text: str, default: Any = None) -> Any:
    """Parse JSON safely; return default on any failure."""
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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Process this report. Use tools as needed.\n\n"
                f"REPORT:\n{json.dumps(report, indent=2)}"
            ),
        },
    ]

    log: List[Dict[str, Any]] = []
    step = 0

    while step < MAX_STEPS:
        step += 1
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.1,
            )
        except Exception as e:
            log.append({
                "step": step,
                "thought": "LLM call failed",
                "action": None,
                "observation": f"LLM ERROR: {type(e).__name__}: {e}",
            })
            break

        msg = response.choices[0].message
        messages.append(msg)

        thought = (msg.content or "").strip() or "(no explicit thought)"

        if not msg.tool_calls:
            log.append({
                "step": step,
                "thought": thought,
                "action": None,
                "observation": "Agent finished without further tool calls.",
            })
            break

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            raw_args = tool_call.function.arguments or "{}"
            args = _safe_json_loads(raw_args, default={})

            if not isinstance(args, dict):
                args = {}

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

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": observation,
            })

            if fn_name == "record_decision":
                pred = _safe_json_loads(observation, default=None)
                if pred is None or not isinstance(pred, dict):
                    pred = args if isinstance(args, dict) else {}
                log.append({"step": step, "final_prediction": pred})
                return log

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
    print(json.dumps(result, indent=2, default=str))