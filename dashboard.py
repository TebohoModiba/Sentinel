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
    # dtype=str keeps everything as text so we don't accidentally lose
    # malformed values (e.g. a broken timestamp) to pandas type coercion.
    return pd.read_csv(csv_path, dtype=str)


@st.cache_data
def load_predictions(jsonl_path):
    predictions = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            predictions.append(json.loads(line))
    return predictions


reports_df = load_reports(REPORTS_CSV)
predictions = load_predictions(PREDICTIONS_JSONL)

if len(predictions) == 0:
    st.error("predictions.jsonl is empty. Run the agent to generate predictions first.")
    st.stop()

# Lookup so we can pull the original raw fields for any report_id
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
    return f"{SEVERITY_ICON.get(sev, '⚪')} {sev}"


def action_summary(actions):
    if not actions:
        return "No new action"
    return ", ".join(a.get("type", "?") for a in actions)


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
        "Relationship": pred.get("relationship"),
        "Severity": severity_badge(pred.get("severity", "")),
        "Confidence": pred.get("confidence"),
        "Action(s)": action_summary(pred.get("actions", [])),
        "Status": pred.get("incident_status"),
        "Human review": "Yes" if pred.get("human_review") else "No",
        "Reason": pred.get("reason", pred.get("explanation", "")),
    }
    log_rows.append(row)

log_df = pd.DataFrame(log_rows)


def highlight_row(row):
    if row["Relationship"] == "CONFLICT":
        return ["background-color: #ffe0e0"] * len(row)
    if row["Human review"] == "Yes":
        return ["background-color: #fff3cd"] * len(row)
    return [""] * len(row)


st.dataframe(
    log_df.style.apply(highlight_row, axis=1),
    use_container_width=True,
    height=420,
)
st.caption("Red rows: conflicting evidence. Yellow rows: flagged for human review.")

st.divider()


# ---------- View 3: Incident Summary ----------

st.header("3. Incident Summary")

# Walk through processed predictions in order, keeping the most recent
# state seen for each incident_id and a running list of its reports.
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

    # These get overwritten each time we see the incident again,
    # so after the loop they hold the LATEST known state.
    info["latest_severity"] = pred.get("severity")
    info["latest_status"] = pred.get("incident_status")
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
                + ", ".join(info["report_ids"])
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
                    st.markdown(f"- {a.get('type')} → {a.get('service_id', 'N/A')}")
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
        action_rows.append({
            "Incident ID": pred.get("incident_id"),
            "Report ID": report_id,
            "Time": ts,
            "Action": a.get("type"),
            "Service": a.get("service_id", "N/A"),
            "Incident status at the time": pred.get("incident_status"),
        })

if action_rows:
    action_df = pd.DataFrame(action_rows)
    st.dataframe(action_df, use_container_width=True, height=420)
else:
    st.info("No actions dispatched yet at this point in the replay.")