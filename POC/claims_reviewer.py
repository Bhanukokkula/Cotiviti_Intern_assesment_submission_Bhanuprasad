"""
Agentic AI claims reviewer proof-of-concept.

For each claim in claims.csv: pick the most relevant policy excerpt with
keyword overlap, ask Claude to review the claim against it, print and log
the decision, then report how many planted anomalies were caught.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-4-6"
CLAIMS_FILE = "claims.csv"
POLICIES_DIR = "policies"
AUDIT_LOG_FILE = "audit_log.jsonl"

# Plain-English descriptions for the CPT/diagnosis codes used in claims.csv.
# In a real system these would come from a CPT/ICD-10 lookup table.
CODE_DESCRIPTIONS = {
    "99213": "office outpatient visit established patient low complexity",
    "99214": "office outpatient visit established patient moderate complexity",
    "99215": "office outpatient visit established patient high complexity",
    "80053": "comprehensive metabolic panel blood test",
    "71046": "chest x-ray two views",
    "97110": "therapeutic exercise physical therapy",
    "90686": "influenza vaccine injection",
    "45378": "diagnostic colonoscopy",
    "73721": "mri lower extremity joint",
    "20610": "arthrocentesis aspiration injection major joint",
    "59400": "obstetric care vaginal delivery routine",
    "I10": "essential hypertension",
    "E11.9": "type 2 diabetes mellitus without complications",
    "E11.65": "type 2 diabetes mellitus with hyperglycemia",
    "J06.9": "acute upper respiratory infection",
    "M54.5": "low back pain",
    "Z23": "encounter for immunization",
    "Z12.11": "encounter for screening for malignant neoplasm of colon",
    "M25.561": "pain in right knee",
    "Z00.00": "general adult medical examination without abnormal findings",
    "S93.401A": "sprain of ankle initial encounter",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "is", "are",
    "must", "be", "in", "on", "with", "that", "this", "claim",
    "claims", "will", "may", "not", "only",
    # "amount" is a generic field label every claim carries with no
    # policy-relevant meaning; excluding it avoids diluting real matches.
    "amount",
}

def load_claims(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def load_policies(folder):
    policies = {}
    for filename in sorted(os.listdir(folder)):
        if filename.endswith(".txt"):
            with open(os.path.join(folder, filename)) as f:
                policies[filename] = f.read()
    return policies

def keyword_overlap(text_a, text_b):
    words_a = set(re.findall(r"[a-z]+", text_a.lower())) - STOPWORDS
    words_b = set(re.findall(r"[a-z]+", text_b.lower())) - STOPWORDS
    return len(words_a & words_b)

def describe_claim(claim, duplicate_of=None):
    cpt_desc = CODE_DESCRIPTIONS.get(claim["cpt_code"], claim["cpt_code"])
    diag_desc = CODE_DESCRIPTIONS.get(claim["diagnosis_code"], claim["diagnosis_code"])
    text = (
        f"{cpt_desc} {diag_desc} units {claim['units']} "
        f"amount {claim['amount']} age {claim['patient_age']}"
    )
    if duplicate_of:
        # A single claim's own fields can never say "I am a duplicate" —
        # that only becomes true in light of another claim. This phrase
        # is added only after find_duplicate() has actually confirmed a
        # match against a prior claim, so it reflects a real finding.
        text += " duplicate submission same provider same date same billed amount"
    return text

def classify_issue(planted_issue):
    """Bucket a claim by its planted_issue label: an exact "clean" claim,
    a "clean_borderline" claim (deliberately ambiguous, not a planted
    anomaly), or a genuinely planted anomaly (anything else)."""
    if planted_issue == "clean":
        return "clean"
    if planted_issue.startswith("clean_borderline"):
        return "borderline"
    return "planted"

def find_duplicate(claim, seen_signatures):
    """Return the claim_id of an earlier claim with identical provider,
    procedure, diagnosis, date, and amount — or None if this is the first
    time this signature has been seen."""
    signature = (claim["provider_id"], claim["cpt_code"], claim["diagnosis_code"],
                 claim["date"], claim["amount"])
    duplicate_of = seen_signatures.get(signature)
    if duplicate_of is None:
        seen_signatures[signature] = claim["claim_id"]
    return duplicate_of

def pick_best_policy(claim, policies, duplicate_of=None):
    claim_text = describe_claim(claim, duplicate_of)
    best_name, best_text, best_score = None, "", -1
    for name, text in policies.items():
        score = keyword_overlap(claim_text, text)
        if score > best_score:
            best_name, best_text, best_score = name, text, score
    return best_name, best_text

def build_prompt(claim, policy_name, policy_text, duplicate_of=None):
    duplicate_note = ""
    if duplicate_of:
        duplicate_note = (
            f"\nSystem note: this claim's provider, procedure code, diagnosis "
            f"code, service date, and billed amount are identical to claim "
            f"{duplicate_of}, which was already processed. This is a likely "
            f"duplicate submission.\n"
        )
    return f"""You are reviewing a healthcare insurance claim for payment integrity.

Claim details:
- claim_id: {claim['claim_id']}
- provider_id: {claim['provider_id']}
- patient_age: {claim['patient_age']}
- cpt_code: {claim['cpt_code']} ({CODE_DESCRIPTIONS.get(claim['cpt_code'], 'unknown procedure')})
- diagnosis_code: {claim['diagnosis_code']} ({CODE_DESCRIPTIONS.get(claim['diagnosis_code'], 'unknown diagnosis')})
- units: {claim['units']}
- amount: {claim['amount']}
- date: {claim['date']}
{duplicate_note}
Most relevant billing policy excerpt ({policy_name}):
\"\"\"{policy_text}\"\"\"

Review this claim against the policy excerpt. Respond with ONLY a JSON object
(no other text) with these exact keys:
- "decision": one of "approve", "flag", or "escalate"
- "reasoning": a short list of strings, your step-by-step reasoning
- "cited_policy_line": the single most relevant line from the policy excerpt

JSON:"""

def call_claude(client, prompt):
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")

def parse_response(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text}")
    return json.loads(match.group(0))

def log_decision(claim_id, decision, reasoning, cited_policy_line, cited_policy_file):
    entry = {
        "claim_id": claim_id,
        "decision": decision,
        "reasoning": reasoning,
        "cited_policy_file": cited_policy_file,
        "cited_policy_line": cited_policy_line,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(AUDIT_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: set the ANTHROPIC_API_KEY environment variable first.")
        sys.exit(1)

    client = anthropic.Anthropic()
    claims = load_claims(CLAIMS_FILE)
    policies = load_policies(POLICIES_DIR)

    categories = {c["claim_id"]: classify_issue(c["planted_issue"]) for c in claims}
    totals = {"planted": 0, "clean": 0, "borderline": 0}
    flagged = {"planted": 0, "clean": 0, "borderline": 0}
    for category in categories.values():
        totals[category] += 1
    seen_signatures = {}

    for claim in claims:
        print(f"\n--- Claim {claim['claim_id']} ---")
        duplicate_of = find_duplicate(claim, seen_signatures)
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

        print(f"Policy used: {policy_name}")
        print(f"Decision: {decision}")
        print("Reasoning:")
        for step in reasoning:
            print(f"  - {step}")
        if cited_policy_line:
            print(f"Cited policy line: {cited_policy_line}")

        log_decision(claim["claim_id"], decision, reasoning, cited_policy_line, policy_name)

        if decision in ("flag", "escalate"):
            flagged[categories[claim["claim_id"]]] += 1

    print(f"\nFlagged {flagged['planted']} of {totals['planted']} planted anomalies.")
    print(f"Clean claims: {flagged['clean']} of {totals['clean']} flagged.")
    print(f"Borderline claims: {flagged['borderline']} of {totals['borderline']} flagged.")

if __name__ == "__main__":
    main()
