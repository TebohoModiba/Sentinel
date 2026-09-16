"""
Batch runner: reads campus_reports.csv, processes each row in order,
appends each decision to predictions.jsonl.
"""
import csv
import json

from dotenv import load_dotenv
load_dotenv()

from agent import process_report
from tools import _coerce_raw_row, reset_state


def main():
    reset_state()

    # Truncate predictions.jsonl so the replay is clean
    open("predictions.jsonl", "w").close()

    with open("campus_reports.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    for i, row in enumerate(rows):
        report = _coerce_raw_row(row, i)
        print(f"[{i+1}/{len(rows)}] {report['report_id']} "
              f"({report['category']}, {report['location']})")

        result = process_report(report)
        pred = result.get("prediction")

        if pred:
            with open("predictions.jsonl", "a", encoding="utf-8") as out:
                out.write(json.dumps(pred, default=str) + "\n")
        else:
            print(f"  ! No decision produced for {report['report_id']}")

    print(f"\nDone. Wrote {len(rows)} decisions to predictions.jsonl.")


if __name__ == "__main__":
    main()