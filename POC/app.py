"""
Streamlit UI for the agentic AI claims reviewer.

Reuses all the logic from claims_reviewer.py (loading, policy retrieval,
prompting, and audit logging) — this file is display and interaction only.
Run with: streamlit run app.py
"""

import json
import os

import anthropic
import streamlit as st

from claims_reviewer import (
    AUDIT_LOG_FILE,
    CLAIMS_FILE,
    POLICIES_DIR,
    build_prompt,
    call_claude,
    classify_issue,
    find_duplicate,
    load_claims,
    load_policies,
    log_decision,
    parse_response,
    pick_best_policy,
)

DECISION_ICON = {"approve": "🟢", "flag": "🟠", "escalate": "🔴", "error": "⚫"}


def load_latest_results():
    """Read audit_log.jsonl and keep only the most recent entry per claim
    (the file is append-only, so later lines override earlier ones)."""
    results = {}
    if not os.path.exists(AUDIT_LOG_FILE):
        return results
    with open(AUDIT_LOG_FILE) as f:
        for line in f:
            entry = json.loads(line)
            results[entry["claim_id"]] = {
                "decision": entry["decision"],
                "reasoning": entry["reasoning"],
                "policy_name": entry["cited_policy_file"],
                "cited_policy_line": entry["cited_policy_line"],
            }
    return results


st.set_page_config(page_title="Claims Reviewer", page_icon="🩺", layout="wide")
st.title("🩺 Agentic AI Claims Reviewer")
st.caption(
    "Keyword-overlap policy retrieval + Claude reasoning + audit trail — "
    "a proof-of-concept for healthcare payment integrity review."
)

claims = load_claims(CLAIMS_FILE)
policies = load_policies(POLICIES_DIR)
categories = {c["claim_id"]: classify_issue(c["planted_issue"]) for c in claims}

# Recompute which claims are duplicates, purely for display — this is
# deterministic and cheap, so it works whether results came from a fresh
# run or were loaded from a previous run's audit log.
seen_signatures = {}
duplicate_of_map = {c["claim_id"]: find_duplicate(c, seen_signatures) for c in claims}

if "results" not in st.session_state:
    st.session_state.results = load_latest_results()

with st.sidebar:
    st.header("Billing policies")
    st.caption("The 3 excerpts Claude retrieves from and cites.")
    chosen = st.selectbox("View policy text", sorted(policies.keys()))
    st.text(policies[chosen])

run_clicked = st.button("▶ Run Review", type="primary")

if run_clicked:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("Set the ANTHROPIC_API_KEY environment variable, then restart Streamlit.")
    else:
        client = anthropic.Anthropic()
        progress = st.progress(0.0, text="Starting review...")
        seen = {}
        for i, claim in enumerate(claims):
            progress.progress(i / len(claims), text=f"Reviewing {claim['claim_id']}...")
            duplicate_of = find_duplicate(claim, seen)
            policy_name, policy_text = pick_best_policy(claim, policies, duplicate_of)
            prompt = build_prompt(claim, policy_name, policy_text, duplicate_of)
            try:
                raw_text = call_claude(client, prompt)
                result = parse_response(raw_text)
                decision = result.get("decision", "unknown")
                reasoning = result.get("reasoning", [])
                cited_policy_line = result.get("cited_policy_line", "")
            except Exception as exc:
                decision = "error"
                reasoning = [f"API or parsing error: {exc}"]
                cited_policy_line = ""

            log_decision(claim["claim_id"], decision, reasoning, cited_policy_line, policy_name)
            st.session_state.results[claim["claim_id"]] = {
                "decision": decision,
                "reasoning": reasoning,
                "policy_name": policy_name,
                "cited_policy_line": cited_policy_line,
            }
        progress.progress(1.0, text="Done")
        st.success("Review complete — audit_log.jsonl updated.")

results = st.session_state.results

if not results:
    st.info("Click **Run Review** to process all 12 claims against Claude.")
else:
    totals = {"planted": 0, "clean": 0, "borderline": 0}
    flagged = {"planted": 0, "clean": 0, "borderline": 0}
    for claim in claims:
        category = categories[claim["claim_id"]]
        totals[category] += 1
        r = results.get(claim["claim_id"])
        if r and r["decision"] in ("flag", "escalate"):
            flagged[category] += 1

    col1, col2, col3 = st.columns(3)
    col1.metric("Planted anomalies caught", f"{flagged['planted']} / {totals['planted']}")
    col2.metric("Clean claims flagged", f"{flagged['clean']} / {totals['clean']}")
    col3.metric("Borderline claims flagged", f"{flagged['borderline']} / {totals['borderline']}")

    st.divider()

    for claim in claims:
        r = results.get(claim["claim_id"])
        if not r:
            st.caption(f"{claim['claim_id']} — not yet reviewed")
            continue

        icon = DECISION_ICON.get(r["decision"], "⚪")
        category = categories[claim["claim_id"]]
        header = (
            f"{icon} **{claim['claim_id']}** — {r['decision'].upper()} "
            f"— {claim['cpt_code']} / {claim['diagnosis_code']} ({category})"
        )
        with st.expander(header, expanded=(claim["claim_id"] in {"C001", "C009", "C010", "C011", "C012"})):
            left, right = st.columns(2)
            with left:
                st.write("**Claim fields**")
                st.json({k: v for k, v in claim.items() if k != "planted_issue"})
                st.write(f"**Ground truth:** `{claim['planted_issue']}`")
                if duplicate_of_map[claim["claim_id"]]:
                    st.write(f"**Duplicate of:** {duplicate_of_map[claim['claim_id']]}")
            with right:
                st.write(f"**Policy retrieved:** `{r['policy_name']}`")
                st.write("**Reasoning:**")
                for step in r["reasoning"]:
                    st.markdown(f"- {step}")
                if r["cited_policy_line"]:
                    st.info(r["cited_policy_line"])
