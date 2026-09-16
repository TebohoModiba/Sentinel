# Sentinel-Campus
Agentic AI Hackathon

------------------------------------------------------------------------------------------------------------------------------------------------------------
## Set-Up and Run Commands

1. Clone the repository and open a terminal in the project root. 
2. Create and activate a virtual environment:
```
(Windows PowerShell)
python-m venv venv
venv\Scripts\Activate.ps1
```

____________________________________________________________________________________________________________________________________________________________

3. Install all of the dependencies:
```
pip install -r requirements
```
____________________________________________________________________________________________________________________________________________________________

4. If the agent needs API keys (e.g. OpenAI or Gemini), create a `.env` file in the project root containing:
```
$env:GEMINI_API_KEY = insert your key here
```

- (Do not commit this file — it should already be listed in `.gitignore`.)
____________________________________________________________________________________________________________________________________________________________

5. Running the agent (generates predictions.jsonl)

With the virtual environment active, run:
```
python agent.py campus_reports.csv
```

- This processes every report in `campus_reports.csv` in file order and writes one JSON object per report to `predictions.jsonl` in the project root.

____________________________________________________________________________________________________________________________________________________________

6. Running the dashboard

Once `predictions.jsonl` exists (either from running the agent above, or from a sample file for testing), start the dashboard with:
```
streamlit run dashboard.py
```

- This opens the dashboard in your browser at `http://localhost:8501`. 
- Use the "Reports processed so far" slider in the sidebar to replay the run report-by-report in the order they were processed.

____________________________________________________________________________________________________________________________________________________________



