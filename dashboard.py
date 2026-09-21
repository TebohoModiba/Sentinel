"""
Sentinel-Campus - Human Dashboard
--------------------------------------
Reads:
  - campus_reports.csv   : the raw reports, in file order
  - predictions.jsonl    : one JSON object per processed report, written by the agent

Shows the four required views (Incoming Report, Decision Log, Incident Summary,
Action History) and lets a judge "replay" the run by dragging a slider through
the reports in the order they were processed.

Run with:
    streamlit run dashboard.py
"""

import streamlit as st
import pandas as pd
import json

st.set_page_config(page_title="Campus Crisis Agent Dashboard", layout="wide")

REPORTS_CSV = "campus_reports.csv"
PREDICTIONS_JSONL = "predictions.jsonl"


# ---------- Data loading ----------

@st.cache_data
def load_reports(csv_path):
    try:
        return pd.read_csv(csv_path, dtype=str)
    except FileNotFoundError:
        st.error(f"Could not find '{csv_path}'. Please make sure the CSV file exists.")
        st.stop()


@st.cache_data
def load_predictions(jsonl_path):
    predictions = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                # Support both raw decision dicts and wrapped {"prediction": {...}}
                if "prediction" in data and isinstance(data["prediction"], dict):
                    predictions.append(data["prediction"])
                else:
                    predictions.append(data)
    except FileNotFoundError:
        return []
    return predictions


reports_df = load_reports(REPORTS_CSV)
predictions = load_predictions(PREDICTIONS_JSONL)

if len(predictions) == 0:
    st.warning("`predictions.jsonl` is empty or missing. Run the agent to generate predictions first.")
    st.stop()

reports_by_id = reports_df.set_index("report_id").to_dict(orient="index")


# ---------- Sidebar: replay control ----------

st.sidebar.title("Replay Control")
total = len(predictions)
step = st.sidebar.slider(
    "Reports processed so far",
    min_value=1,
    max_value=total,
    value=total,
)
st.sidebar.caption(f"Showing agent state after processing {step} of {total} reports.")

processed = predictions[:step]
current = processed[-1]


# ---------- Helpers ----------

SEVERITY_ICON = {
    "LOW": "🟢",
    "MEDIUM": "🟡",
    "HIGH": "🟠",
    "CRITICAL": "🔴",
}

def severity_badge(sev):
    key = str(sev or "").upper()
    return f"{SEVERITY_ICON.get(key, '⚪')} {key}"


def action_summary(actions):
    if not actions:
        return "No new action"
    return ", ".join(a.get("type", "?") for a in actions if isinstance(a, dict))


def pretty_relationship(rel):
    """Map canonical lowercase -> Title Case for display."""
    if not rel:
        return "-"
    return str(rel).replace("_", " ").title()


# ---------- View 1: Incoming Report ----------

st.header("1. Incoming Report")
current_report_id = current.get("report_id")
raw = reports_by_id.get(current_report_id, {})

col1, col2 = st.columns(2)
with col1:
    st.markdown(f"**Report ID:** {current_report_id}")
    st.markdown(f"**Timestamp:** {raw.get('timestamp', 'N/A')}")
    st.markdown(f"**Location:** {raw.get('location', 'N/A')}")
    st.markdown(f"**Reporter type:** {raw.get('reporter_type', 'N/A')}")
with col2:
    st.markdown(f"**Category (as reported):** {raw.get('category', 'N/A')}")
    st.markdown(f"**Severity (as reported):** {raw.get('reported_severity', 'N/A')}")
    st.markdown(f"**Decision label:** {current.get('decision', 'N/A')}")

st.markdown(f"**Description:** {raw.get('description', 'N/A')}")

if current.get("human_review"):
    st.warning("This report's decision has been flagged for human review.")

st.divider()


# ---------- View 2: Decision Log ----------

st.header("2. Decision Log")

log_rows = []
for pred in processed:
    row = {
        "Report ID": pred.get("report_id"),
        "Incident ID": pred.get("incident_id"),
        "Relationship": pretty_relationship(pred.get("relationship")),
        "Decision": pred.get("decision", "-"),
        "Severity": severity_badge(pred.get("severity", "")),
        "Confidence": pred.get("confidence"),
        "Service": pred.get("service", "-"),
        "Action(s)": action_summary(pred.get("actions", [])),
        "Status": pred.get("incident_status", pred.get("status")),
        "Human review": "Yes" if pred.get("human_review") else "No",
        "Reason": pred.get("reason", pred.get("explanation", "")),
    }
    log_rows.append(row)

log_df = pd.DataFrame(log_rows)


def highlight_row(row):
    rel = str(row["Relationship"]).lower()
    if "conflict" in rel:
        return ["background-color: #ffe0e0"] * len(row)
    if row["Human review"] == "Yes":
        return ["background-color: #fff3cd"] * len(row)
    return [""] * len(row)


# NOTE: st.dataframe() pulls in pyarrow under the hood, which can be blocked
# by Application Control / WDAC policies on locked-down machines. Rendering
# the styled table as HTML avoids pyarrow entirely while keeping the same
# row highlighting and layout.
styled_html = (
    log_df.style
    .apply(highlight_row, axis=1)
    .set_table_styles([
        {"selector": "th", "props": [("text-align", "left"), ("padding", "6px 10px")]},
        {"selector": "td", "props": [("padding", "6px 10px")]},
    ])
    .set_properties(**{"font-size": "0.9rem"})
    .to_html()
)

st.markdown(
    f'<div style="max-height:420px; overflow:auto;">{styled_html}</div>',
    unsafe_allow_html=True,
)
st.caption("Red rows: conflicting evidence. Yellow rows: flagged for human review.")

st.divider()


# ---------- View 3: Incident Summary ----------

st.header("3. Incident Summary")

incidents = {}
for pred in processed:
    inc_id = pred.get("incident_id")
    if not inc_id:
        continue

    if inc_id not in incidents:
        incidents[inc_id] = {"report_ids": [], "severity_history": []}

    info = incidents[inc_id]
    info["report_ids"].append(pred.get("report_id"))
    info["severity_history"].append(pred.get("severity"))

    info["latest_severity"] = pred.get("severity")
    info["latest_status"] = pred.get("incident_status", pred.get("status"))
    info["latest_confidence"] = pred.get("confidence")
    info["latest_actions"] = pred.get("actions", [])
    info["latest_reason"] = pred.get("reason", pred.get("explanation", ""))

    raw_current = reports_by_id.get(pred.get("report_id"), {})
    info["location"] = raw_current.get("location", "Unknown")
    info["category"] = raw_current.get("category", "Unknown")

if not incidents:
    st.info("No incidents opened yet at this point in the replay.")
else:
    for inc_id, info in incidents.items():
        title = f"{inc_id} — {severity_badge(info['latest_severity'])} — {info['latest_status']}"
        with st.expander(title):
            st.markdown(f"**Category:** {info['category']}")
            st.markdown(f"**Location:** {info['location']}")
            st.markdown(f"**Current confidence:** {info['latest_confidence']}")
            st.markdown(
                f"**Reports linked ({len(info['report_ids'])}):** "
                + ", ".join(str(r) for r in info["report_ids"])
            )
            st.markdown(
                "**Severity over time:** "
                + " → ".join(str(s) for s in info["severity_history"])
            )
            if info["latest_reason"]:
                st.markdown(f"**Latest reasoning:** {info['latest_reason']}")
            if info["latest_actions"]:
                st.markdown("**Current action(s):**")
                for a in info["latest_actions"]:
                    if isinstance(a, dict):
                        st.markdown(
                            f"- {a.get('type', '?')} → "
                            f"{a.get('service_name') or a.get('service_id', 'N/A')}"
                        )
                    else:
                        st.markdown(f"- {a}")
            else:
                st.markdown("**Current action(s):** None (existing response still sufficient)")

st.divider()


# ---------- View 4: Action History ----------

st.header("4. Action History")

action_rows = []
for pred in processed:
    report_id = pred.get("report_id")
    raw_current = reports_by_id.get(report_id, {})
    ts = raw_current.get("timestamp", "N/A")
    actions = pred.get("actions", [])

    if not actions:
        continue

    for a in actions:
        if not isinstance(a, dict):
            a = {"type": str(a)}
        action_rows.append({
            "Incident ID": pred.get("incident_id"),
            "Report ID": report_id,
            "Time": ts,
            "Action": a.get("type", "?"),
            "Service": a.get("service_name") or a.get("service_id", "N/A"),
            "Incident status at the time": pred.get("incident_status", pred.get("status")),
        })

if action_rows:
    action_df = pd.DataFrame(action_rows)
    # Same pyarrow-avoidance as the Decision Log above.
    st.markdown(
        f'<div style="max-height:420px; overflow:auto;">{action_df.to_html(index=False)}</div>',
        unsafe_allow_html=True,
    )
else:
    st.info("No actions dispatched yet at this point in the replay.")