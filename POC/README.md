# Agentic AI Claims Reviewer (Hackathon PoC)

Demonstrates an agentic AI reviewer for healthcare payment integrity: for
each synthetic claim, a plain keyword-overlap function (no vector database)
picks the most relevant billing policy excerpt, then Claude reviews the
claim against it and returns a decision, step-by-step reasoning, and a
cited policy line. Every decision is logged to `audit_log.jsonl` for
audit trail. `claims.csv` hides a `planted_issue` column marking 4 known
problems (duplicate, upcoding, excessive units, implausible code) so you
can see how many the agent catches.

## Run it (command line)

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=your-key-here
    python claims_reviewer.py

## Run it (Streamlit UI)

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=your-key-here
    streamlit run app.py

Opens a browser tab with a "Run Review" button, live progress, summary
metrics for the planted/clean/borderline buckets, and an expandable
per-claim view of the decision, reasoning, and cited policy line. On
load it also shows results from the last `audit_log.jsonl` run, if one
exists, without needing to click Run Review again.

## Screenshots

Summary view after a run — 4/4 planted anomalies caught, 0/7 clean claims
misflagged, 1/1 borderline claim flagged:

![Run summary](Streamlit_Screenshots/Screenshot%202026-08-07%20at%206.47.50%20PM.png)

Clean claim approved (99213 / I10) — no upcoding indicators found:

![Clean claim approved](Streamlit_Screenshots/Screenshot%202026-08-07%20at%206.46.53%20PM.png)

Planted upcoding flagged (99215 billed against a routine Z00.00 exam):

![Upcoding flagged](Streamlit_Screenshots/Screenshot%202026-08-07%20at%206.47.21%20PM.png)

Planted excessive-units claim escalated (25 units of a joint injection
normally billed as 1):

![Excessive units escalated](Streamlit_Screenshots/Screenshot%202026-08-07%20at%206.47.36%20PM.png)
