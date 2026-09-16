# Development Data Dictionary

**STUDENT-FACING**

| Field | Meaning |
|---|---|
| report_id | Unique identifier for the report, not the incident |
| timestamp | Reported arrival time; process file order |
| location | Reporter-provided location text |
| category | Reporter-provided category and therefore not guaranteed correct |
| reported_severity | Reporter-provided severity and therefore not ground truth |
| description | Free-text report evidence |
| reporter_type | General source type; not a reliability guarantee |

All records are synthetic. The same underlying incident may appear in many rows.
