
```markdown
# Sentinel - Campus Agentic AI

## Set-Up and Run Commands

### 1. Clone the Repository
Clone the repository and navigate to the project root directory in your terminal.

### 2. Create and Activate Virtual Environment
**(Windows PowerShell)**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1

```

### 3. Install Dependencies

```bash
pip install -r requirements.txt

```

### 4. Configure Environment Variables

If the agent needs API keys, create a `.env` file in the project root:

```dotenv
GEMINI_API_KEY=insert_your_key_here
AGENT_MODEL=gemini-3.1-flash-lite

```

> **Note:** A template is provided at `.env.example`. Do **not** commit `.env` to version control (it is included in `.gitignore`).

### 5. Run the Agent

With the virtual environment active, run the batch script to generate `predictions.jsonl`:

```bash
python run_agent.py

```

*This processes every report in `campus_reports.csv` in sequential order and writes one JSON object per report to `predictions.jsonl` in the root directory.*

### 6. Run the Dashboard

Once `predictions.jsonl` exists (either generated above or from a test sample), launch the Streamlit dashboard:

```bash
python -m streamlit run dashboard.py

```

* Access the dashboard in your browser at `http://localhost:8501`.
* Use the **"Reports processed so far"** slider in the sidebar to replay the run report-by-report in the order processed.

---

## Troubleshooting

### `ImportError: cannot import name 'utils' from 'models' (unknown location)`

* **Cause:** Python doesn't recognize the `models/` folder as a package, causing `from models import utils` in `tools.py` to fail.
* **Fix:** Create an empty `__init__.py` file inside the `models/` directory:
```text
Sentinel/
└── models/
    ├── __init__.py  ← Create this (empty file)
    └── utils.py

```


**PowerShell Command:**
```powershell
New-Item -Path "models\__init__.py" -ItemType File

```


If the error persists, clear stale bytecode caches:
```powershell
Remove-Item -Recurse -Force __pycache__
Remove-Item -Recurse -Force models\__pycache__ -ErrorAction SilentlyContinue

```



---

### `Import "rapidfuzz" could not be resolved`

* **Cause:** Required dependencies (`rapidfuzz`, `python-dateutil`) used by `utils.py` are missing from your active virtual environment.
* **Fix:** Install them directly or via requirements:
```powershell
pip install rapidfuzz python-dateutil
# OR
pip install -r requirements.txt

```


**Verify installation:**
```powershell
python -c "import rapidfuzz, dateutil; print('deps OK')"

```


*If installation succeeds but the error persists, verify your active interpreter:*
```powershell
python -c "import sys; print(sys.executable)"

```


*Ensure the path includes `.venv`. If not, activate it first:*
```powershell
.\venv\Scripts\Activate.ps1

```



---

### `429 RESOURCE_EXHAUSTED / Quota exceeded`

* **Cause:** You hit the Gemini free-tier daily request limit. Quotas apply per **Google Cloud project**, so generating additional keys under the same project will not reset the limit.

| Model | Requests / Day (Free Tier) |
| --- | --- |
| `gemini-3.6-flash` | 20 |
| `gemini-2.5-flash` | 20 |
| `gemini-3.1-flash-lite` | 500 |

* **Fix:** Switch to `gemini-3.1-flash-lite` in your `.env` file:
```dotenv
AGENT_MODEL=gemini-3.1-flash-lite

```


Verify the update:
```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('AGENT_MODEL'))"

```


* **Important Notes:**
* Daily quotas reset at **Midnight Pacific Time (PT)**.
* Each team member should generate an API key from their own distinct Google Cloud project at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) to avoid sharing quotas.
* Free-tier requests may be processed for product improvement—do not use real sensitive data.



---

### `Windows blocks streamlit.exe: An Application Control policy has blocked this file`

* **Cause:** Windows Smart App Control (or corporate policy) blocks unsigned executables generated inside `.venv/Scripts/`.
* **Fix:** Launch Streamlit directly via the Python module:
```powershell
python -m streamlit run dashboard.py

```


* **Alternative:** Create a script named `run_dashboard.cmd` in the root folder with:
```batch
@echo off
python -m streamlit run dashboard.py

```


Double-click `run_dashboard.cmd` to start the app.
* **Last Resort:** Turn off Smart App Control under *Windows Security → App & browser control → Smart App Control settings*. *(Warning: Disabling this permanently requires a Windows reinstall to re-enable).*

---

### `can't open file 'run_agent.py': [Errno 2] No such file or directory`

* **Cause:** `run_agent.py` does not exist in the root directory or terminal is in the wrong location.
* **Fix:** Verify your working directory and check python files:
```powershell
Get-ChildItem *.py

```


Expected files: `agent.py`, `dashboard.py`, `tools.py`, and `run_agent.py`. Ensure `run_agent.py` is present in the root folder.

---

### PowerShell shows a `>>` continuation prompt

* **Cause:** An unclosed quote or multiline command string was entered. PowerShell is awaiting input termination.
* **Fix:** Press `Ctrl + C` to break out and return to the primary prompt (`>`).
To cleanly delete files without syntax errors if they don't exist:
```powershell
Remove-Item predictions.jsonl -ErrorAction SilentlyContinue

```



---

### `predictions.jsonl` is empty or missing

* **Cause:** The agent hasn't been run, or an error terminated execution before outputs were generated.
* **Fix:** Clear partial output files and re-run:
```powershell
Remove-Item predictions.jsonl -ErrorAction SilentlyContinue
python run_agent.py

```


* **Streamlit Caching Note:** `dashboard.py` uses `@st.cache_data`. If the dashboard is running while generating new predictions in another window, kill the Streamlit session (`Ctrl + C`) and restart it to refresh data.

---

### `python agent.py` writes only one prediction

* **Cause:** `agent.py` contains a test block designed to evaluate a single report (`R018`).
* **Fix:** Execute the full batch entry point script instead:
```powershell
python run_agent.py

```



---

## 📄 Environment Variable Template (`.env.example`)

Create a `.env.example` file in your repository with the following contents:

```dotenv
GEMINI_API_KEY=your_key_here
AGENT_MODEL=gemini-3.1-flash-lite

```

```

```
