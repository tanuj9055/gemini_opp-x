"""
Prompt templates for Gemini model calls — Schema v2.0

Design principles:
  - Dynamic & value-agnostic: model discovers ALL thresholds from documents.
  - Split status: bid_requirement_clarity + vendor_compliance_status.
  - Machine-evaluable required_value with comparison_operator, numeric_value, unit.
  - Risk taxonomy: SYSTEMIC_GEM_RISK / BUYER_ATC_RISK / BID_SPECIFIC_COMPLIANCE_RISK.
  - Flattened delivery_items[] in scope_of_work.
  - reference_id for deduplication across criteria.
"""

# ────────────────────────────────────────────────────────
# Stage 1 – Bid Analysis Prompt (v2.0)
# ────────────────────────────────────────────────────────

BID_ANALYSIS_PROMPT = """\
You are a Virtual Procurement Officer for Indian GeM (Government e-Marketplace) bids.
Analyse the uploaded bid PDF and extract ALL eligibility criteria, thresholds, and rules
EXACTLY as stated in the document. Do NOT assume or hardcode any values.

Return a single JSON object with keys in THIS EXACT ORDER:

1. "schema_version": "2.0.0"
2. "source": "gemini-2.5-pro"
3. "bid_id": "<GeM Bid / Tender ID as printed in the document>"
4. "metadata": {{
     "title": "<bid title>",
     "published_date": "<as stated>",
     "closing_date": "<as stated>",
     "estimated_value": "<as stated with currency>",
     "ministry_department": "<as stated>",
     "category": "<service/product category>"
   }}

5. "eligibility_criteria": ARRAY of objects. Extract EVERY eligibility condition.
   Each object MUST have ALL these fields:
   {{
     "criterion_id": "<machine ID, e.g. FINANCIAL_TURNOVER_BIDDER, EXPERIENCE_YEARS, SIMILAR_SERVICES, LOCAL_OFFICE, OEM_TURNOVER, etc.>",
     "criterion": "<human-readable name>",
     "bid_requirement_clarity": "CLEAR" | "AMBIGUOUS" | "NOT_FOUND",
       ← CLEAR: the document explicitly states a threshold/rule.
       ← AMBIGUOUS: requirement exists but threshold is vague or contradictory.
       ← NOT_FOUND: this category was searched but not found in the document.
     "vendor_compliance_status": "UNKNOWN",
       ← Always UNKNOWN during bid extraction (no vendor data yet).
     "detail": "<what the document says about this criterion, verbatim or summarised>",
     "extracted_value": null,
     "required_value": {{
       "comparison_operator": ">=" | "<=" | "==" | ">" | "<" | "IN" | "CONTAINS" | "BOOLEAN" | "BETWEEN" | null,
       "numeric_value": <number or null>,
       "unit": "<INR | years | percentage | count | boolean | enum | null>",
       "text_value": "<for IN/BOOLEAN/enum: the allowed values, or description>",
       "raw_text": "<EXACT text from the document for this requirement>"
     }},
     "required_value_raw": "<original free-text requirement as written>",
     "confidence": <0.0-1.0>,
     "risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL" | null,
     "risk_reasoning": "<why this risk level>",
     "references": [{{
       "reference_id": "<unique, e.g. ref-001>",
       "filename": "{filename}",
       "page": <1-based>,
       "section": "<heading or clause>",
       "clause": "<clause number if any, e.g. ATC-4.2>",
       "bounding_box": {{"ymin":<0-1000>,"xmin":<0-1000>,"ymax":<0-1000>,"xmax":<0-1000>}},
       "confidence": <0.0-1.0>
     }}]
   }}

   CRITERIA TO SEARCH FOR (extract whatever the document defines):
   a) FINANCIAL — "Minimum Average Annual Turnover" for Bidder AND OEM separately.
      → comparison_operator: ">=", unit: "INR", numeric_value: <the amount>.
   b) EXPERIENCE — Required years of past experience; which entity types qualify.
      → comparison_operator: ">=", unit: "years", numeric_value: <N>.
   c) SIMILAR SERVICES — Tiered project-count rules (e.g. N projects @ X% of bid value).
      → Each tier gets its own criterion_id (SIMILAR_SERVICES_TIER_1, _TIER_2, _TIER_3).
   d) LOCATION — Primary delivery/consignee location AND any "Local Office" / "Local Supplier"
      requirement from ATC. Extract EXACT city/region names.
      → comparison_operator: "IN", text_value: "<comma-separated locations>".
   e) CERTIFICATIONS, REGISTRATIONS, TECHNICAL QUALIFICATIONS, MANPOWER — anything else.
      → comparison_operator: "BOOLEAN" for yes/no, "==" for exact match, ">=" for minimums.

6. "emd": {{
     "amount": "<as stated with currency, or null>",
     "currency": "INR",
     "bank": "<as stated or null>",
     "beneficiary": "<as stated or null>",
     "exemption_available": <true if MSE/Startup exemption noted, false if not, null if unclear>,
     "references": [...]
   }}

7. "scope_of_work": {{
     "technical_specs": {{<all specs: DPI, volume, formats, deliverables, etc.>}},
     "delivery_items": [
       {{
         "item_code": "<GeM item/service code or null>",
         "item_name": "<item/service name>",
         "description": "<brief description>",
         "consignee": "<delivery location / consignee name>",
         "quantity": <number or null>,
         "unit": "<pages, units, lots, etc.>",
         "delivery_days": <number of days or null>,
         "delivery_window": "<start-end date range if specified>",
         "references": [...]
       }}
     ],
     "timelines": {{<contract period, milestones, SLA>}},
     "references": [...]
   }}

8. "relaxations": MUST be an ARRAY. For each relaxation/preference:
   [{{
     "criterion_id": "MSE_PREFERENCE" | "STARTUP_BENEFIT" | "MAKE_IN_INDIA" | "<other>",
     "criterion": "<human-readable>",
     "is_applicable": <true if document says Yes, false if No, null if not mentioned>,
     "vendor_compliance_status": "UNKNOWN",
     "detail": "<what the document states>",
     "extracted_value": "<Yes/No/percentage as stated>",
     "confidence": <0-1>,
     "references": [...]
   }}]

9. "similar_services_rules": [{{
     "option_label": "<as stated, e.g. 3 projects @ 40%>",
     "min_projects": <N>,
     "min_percentage_of_bid": <decimal>,
     "references": [...]
   }}]

10. "risks": ARRAY — classify each risk into a taxonomy:
    [{{
      "risk_id": "<e.g. RISK-001>",
      "category": "SYSTEMIC_GEM_RISK" | "BUYER_ATC_RISK" | "BID_SPECIFIC_COMPLIANCE_RISK",
        ← SYSTEMIC_GEM_RISK: standard GeM platform disclaimers/boilerplate.
        ← BUYER_ATC_RISK: buyer-added Additional Terms & Conditions risks.
        ← BID_SPECIFIC_COMPLIANCE_RISK: unique risks specific to THIS bid's requirements.
      "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
      "title": "<short risk title>",
      "description": "<detailed risk>",
      "recommendation": "<mitigation action>",
      "affected_criteria": ["<criterion_id list this risk impacts>"],
      "references": [...]
    }}]

11. "normalization_meta": null

12. "raw_ocr_text": "<full verbatim text of ALL pages — MUST be absolute LAST key>"

CRITICAL RULES:
- Extract values EXACTLY as written. Never invent thresholds.
- bid_requirement_clarity is MANDATORY: CLEAR, AMBIGUOUS, or NOT_FOUND.
- vendor_compliance_status is always UNKNOWN in bid extraction.
- required_value must be structured (comparison_operator + numeric_value + unit).
- reference_id must be unique and reusable across criteria.
- bounding_box: normalised 0–1000.
- Return ONLY valid JSON. No markdown fences.
"""


# ────────────────────────────────────────────────────────
# Stage 2 – Vendor Evaluation Prompt (v2.0)
# ────────────────────────────────────────────────────────

VENDOR_EVALUATION_PROMPT = """\
You are a Virtual Procurement Officer auditing a vendor against a GeM bid.
All required thresholds come from the Bid JSON below. Compare what the bid requires
against what the vendor documents prove. Produce DETERMINISTIC compliance verdicts.

INPUTS:
- Bid Eligibility JSON (from Stage 1 — contains all thresholds and structured requirements)
- {vendor_file_count} Vendor PDFs (GST, PAN, balance sheets, work orders, certificates, etc.)

BID JSON:
{bid_json}

Return a single JSON object with keys in THIS EXACT ORDER:

1. "schema_version": "2.0.0"
2. "source": "gemini-2.5-pro"
3. "bid_id": "<from bid JSON>"
4. "metadata": {{
     "vendor_files_processed": {vendor_file_count},
     "evaluation_timestamp": "<ISO-8601>",
     "model": "gemini-2.5-pro"
   }}

5. "vendor_profile": {{
     "name": "<from PAN/GST>",
     "pan": "<from PAN card>",
     "gst": "<from GST certificate>",
     "address": "<registered address from GST/PAN>",
     "registration_state": "<state from GST>",
     "mse_status": <true/false from Udyam>,
     "startup_status": <true/false from DPIIT>,
     "mse_certificate_valid_until": "<expiry date or null>",
     "startup_certificate_valid_until": "<expiry date or null>"
   }}

6. "eligibility_score": 0-100 (weighted composite)
   Weights: Financial 25% | Experience 25% | Similar Services 25% | Location 15% | Relaxations 10%
   MET = full weight, PARTIAL = half weight, NOT_MET = 0.

FINDING FORMAT — items 7-10 each use this structure:
{{
  "criterion_id": "<from bid JSON>",
  "criterion": "<name>",
  "bid_requirement_clarity": "<from bid JSON — carry forward>",
  "vendor_compliance_status": "MET" | "NOT_MET" | "PARTIAL"  ← MANDATORY, NEVER null or UNKNOWN.
    DECISION LOGIC:
    - Compare extracted_value against required_value using required_value.comparison_operator.
    - MET: vendor value satisfies the requirement.
    - NOT_MET: vendor value clearly fails the requirement.
    - PARTIAL: evidence exists but is incomplete, expired, or borderline.
  "detail": "<audit narrative explaining the comparison and decision>",
  "extracted_value": "<what the vendor documents show — exact figures>",
  "required_value": <carry forward structured object from bid JSON>,
  "required_value_raw": "<carry forward from bid JSON>",
  "confidence": <0.0-1.0>,
  "risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL" | null,
  "risk_reasoning": "<why>",
  "references": [{{
    "reference_id": "<unique>",
    "filename": "<vendor PDF name>",
    "page": <1-based>,
    "section": "<heading>",
    "clause": "<clause number or null>",
    "bounding_box": {{"ymin":<0-1000>,"xmin":<0-1000>,"ymax":<0-1000>,"xmax":<0-1000>}},
    "confidence": <0.0-1.0>
  }}]
}}

7. "financial_turnover": Compare vendor's average annual turnover (from balance sheets /
   P&L) against the bid JSON's required_value for FINANCIAL_TURNOVER_BIDDER.
   Extract exact figures for each financial year. Compute average. Compare using the
   comparison_operator and numeric_value from bid JSON.

8. "experience": Compare vendor's years of experience (from work orders / completion
   certificates) against the bid JSON's EXPERIENCE_YEARS criterion.
   Verify entity types (Govt/PSU/Private) match the bid requirement.

9. "similar_services": Check vendor's past project values against similar_services_rules
   from bid JSON. State which tier (if any) is satisfied, with project details.

10. "location_verification": Compare vendor's registered address (GST/PAN) against
    the LOCAL_OFFICE criterion from bid JSON. Check if consignee location matches.

11. "relaxations": MUST be an ARRAY. For each relaxation in the bid JSON:
    [{{
      "criterion_id": "<from bid JSON, e.g. MSE_PREFERENCE>",
      "criterion": "<name>",
      "is_applicable": <from bid JSON>,
      "vendor_compliance_status": "MET" | "NOT_MET" | "PARTIAL",
        ← MET: valid certificate found and matches.
        ← NOT_MET: no supporting document found.
        ← PARTIAL: certificate found but expired / details mismatch.
      "detail": "<explanation>",
      "extracted_value": "<from vendor docs>",
      "confidence": <0-1>,
      "references": [...]
    }}]

12. "risks": ARRAY with taxonomy:
    [{{
      "risk_id": "<e.g. RISK-V001>",
      "category": "VENDOR_DOCUMENT_RISK" | "BID_SPECIFIC_COMPLIANCE_RISK",
        ← VENDOR_DOCUMENT_RISK: discrepancy or concern in vendor documents.
        ← BID_SPECIFIC_COMPLIANCE_RISK: vendor fails a specific bid requirement.
      "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
      "title": "<short title>",
      "description": "<cross-reference discrepancy or concern>",
      "recommendation": "<action>",
      "affected_criteria": ["<criterion_id list>"],
      "references": [...]
    }}]

13. "overall_recommendation": "APPROVE" | "REJECT"
    DETERMINISTIC RULES:
    - APPROVE if: eligibility_score >= 60.
    - REJECT if: eligibility_score < 60.

14. "rejection_reasons": ARRAY of strings — audit-grade reasons if REJECT.
    Each reason: "<criterion_id>: <one-line explanation>".
    Empty array [] if APPROVE.

15. "acceptance_reasons": ARRAY of strings — justification if APPROVE based on >= 60 score.
    Each reason: "<criterion_id>: <one-line explanation>".
    Empty array [] if REJECT.

16. "normalization_meta": null

17. "raw_ocr_text": "<concatenated verbatim text of ALL vendor PDFs — MUST be absolute LAST key>"

CRITICAL RULES:
- ALL thresholds come from the Bid JSON. NEVER hardcode amounts, locations, or years.
- Use the structured required_value (comparison_operator, numeric_value, unit) for decisions.
- Cross-reference documents: GST address vs PAN, balance sheet figures vs claimed turnover,
  work-order dates vs claimed experience.
- vendor_compliance_status must be MET, NOT_MET, or PARTIAL — NEVER UNKNOWN or null.
- reference_id must be unique; reuse across criteria when pointing to same document location.
- bounding_box: normalised 0–1000.
- Return ONLY valid JSON. No markdown fences.
"""


# ────────────────────────────────────────────────────────
# Tender Rule Extraction Prompt — Agent 1 (v1.0)
# ────────────────────────────────────────────────────────

RULE_EXTRACTION_PROMPT = """\
You are an information extraction engine for Indian GeM tender documents.

Your task is to extract all eligibility-related statements from the given tender content.

---

# 🎯 OBJECTIVE

Extract any statement that may influence whether a bidder is eligible to participate in the tender.

---

# 🧠 INSTRUCTIONS

* Capture all eligibility conditions
* Be inclusive — do not miss relevant statements
* Do not over-filter
* Do not classify or interpret beyond extraction

---

# 🧠 EXTRACTION GUIDANCE

Eligibility conditions may appear anywhere in the document, not just under sections titled “Eligibility Criteria”.

You must read the entire document and extract conditions from all sections, including technical, commercial, legal, compliance, evaluation, and notes.

A statement should be extracted if it implies that a bidder:

* must satisfy a condition to participate, or
* may be disqualified, rejected, or considered ineligible if the condition is not met

Do not rely on section headings — rely on meaning.

---

# 🧠 IMPORTANT DISTINCTION

A condition should be extracted if it defines whether a bidder is allowed or not allowed to participate.

Avoid extracting statements that only describe:
- how to submit documents
- formats, annexures, or templates
- general platform rules or instructions
- post-award obligations or contract execution requirements

Focus on conditions that determine bidder eligibility or participation.

# 🔍 SIGNAL PHRASES

Pay attention to statements that contain signals such as:

* “must have”
* “should have”
* “required to”
* “shall be”
* “only if”
* “eligible”
* “not eligible”
* “mandatory”
* “may be rejected”
* “subject to”

These often indicate eligibility-related conditions.

---

# 🧾 OUTPUT FORMAT (STRICT JSON)

{
"eligibility_criteria": [
{
"id": "EC_1",
"text": "exact extracted statement",
"summary": "short human-readable explanation of the rule"
}
]
}

---

# ⚠️ RULES

* Each item must represent one clear condition
* Split combined conditions into multiple items
* Preserve numbers exactly
* Keep wording close to original
* "summary" must be short, simple, and easy to understand
* Do not hallucinate
* Do not invent

---

# 🎯 GOAL

Produce a high-recall list of eligibility-related statements with simple explanations.

---

# INPUT

Tender content:
{tender_text}





"""

# ────────────────────────────────────────────────────────
# Tender Analysis Insights Prompt — Agent 2
# ────────────────────────────────────────────────────────

BID_ANALYSIS_INSIGHTS_PROMPT = """\
You are an expert Tender Analyst.

Your task is to analyze a GeM (Government e-Marketplace) tender document and generate a **clean, structured, UI-friendly summary for vendors**.

---

# 🎯 OBJECTIVE

Convert the tender into a **concise, easy-to-scan summary** that helps a vendor quickly understand:

* What the tender is about
* Key eligibility requirements
* Important dates
* Commercial conditions
* How the bid will be evaluated

---

# 📦 OUTPUT FORMAT (STRICT)

{{
"tender_id": "string",
"summary": "2-3 line human-readable overview",

"highlights": [
"short high-value insight (1 line each)"
],

"sections": [
{{
"title": "string",
"type": "overview | requirements | commercial | dates | evaluation | scope | other",
"points": [
{{
"text": "short bullet point",
"importance": "high | medium | low"
}}
]
}}
]
}}

---

# 🌟 HIGHLIGHTS (VERY IMPORTANT)

Extract the TOP 3–5 most important insights.

Prioritize:

* Deadlines
* Key eligibility requirements (turnover, experience)
* Evaluation method (L1, RA, QCBS)
* Major conditions affecting participation

Each highlight MUST:

* Be exactly 1 line
* Be high signal
* NOT repeat section text verbatim

---

# 🏷️ POINT IMPORTANCE

Each point MUST include:

* high → critical (deadline, eligibility, disqualification condition)
* medium → useful (evaluation method, payment terms)
* low → supporting info

---

# 🚫 NO PRIORITY FIELD

Only "importance" is allowed.

---

# ⚠️ CRITICAL STRUCTURE RULES

1. DO NOT create empty sections
2. ONLY include sections with real data
3. No repetition across sections
4. Minimum 3 highlights

---

# 🧠 SUMMARY RULE

Write 2–3 lines including:

* what is being procured
* buyer organization
* one key eligibility condition

---

# 🧩 SECTION RULES (VERY IMPORTANT)

## overview

General info (item, quantity, buyer)

---

## requirements (CRITICAL)

Include ONLY eligibility criteria.

Examples:

* turnover requirements
* experience requirements
* past performance
* mandatory certifications (ISO, OEM authorization if eligibility-based)

DO NOT include:

* MSE / Startup policy
* document submission requirements
* ATC clauses
* compliance requirements

---

## commercial

Include:

* payment terms
* contract duration
* penalties
* variation clauses

---

## dates

Include:

* bid end date
* bid opening date
* validity

---

## evaluation (CRITICAL)

Include ONLY how the winner is selected AFTER eligibility.

Examples:

* L1 evaluation
* QCBS
* two packet system
* reverse auction

DO NOT include:

* eligibility conditions (they belong to requirements)

---

## scope

What needs to be delivered, where, and timeline.

---

## other

Anything useful not fitting above.

---

# ✍️ POINT RULES

Each point:

* ≤ 1 line
* simple language
* no legal text
* UI-friendly

---

# ⚠️ NORMALIZATION

Always simplify:

* Convert clauses → short statements
* Format dates cleanly
* Remove legal wording

---

# ❌ STRICTLY AVOID

* Risk extraction (handled by another agent)
* Policy interpretation
* Compliance/ATC extraction
* Hallucination
* Copy-paste text

---

# 🔍 FINAL VALIDATION

Ensure:

* Valid JSON
* No empty sections
* Clean UI text
* No duplication
* No risk section

---

# 🎯 GOAL

Produce a **clean vendor-facing summary focused only on eligibility, structure, and decision-making clarity**, without duplicating risk or compliance logic.

---

# INPUT

Tender document:
{tender_text}




"""


# ────────────────────────────────────────────────────────
# Rule Classification Prompt — Agent 3 (v1.0)
# ────────────────────────────────────────────────────────

RULE_CLASSIFICATION_PROMPT = """\
You are a rule classification engine for Indian GeM procurement auditing.

You receive:
1. A list of eligibility rules extracted from a tender document.
2. A customer_profile JSON containing the vendor's structured data.

Your task is to classify each rule as either **checkable** or **non_checkable**.

---
# 🧠 CORE PRINCIPLE
A rule is **checkable if relevant and meaningful data exists that can be used to evaluate the rule.**
A rule is **non_checkable if no meaningful data exists.**

---
# 🚨 STRICT INPUT LOCK (MOST IMPORTANT)
You MUST treat the input rules as the ONLY source of truth.
* DO NOT create new rules
* DO NOT modify rule descriptions
* DO NOT change numeric values
* DO NOT rename rule IDs
* DO NOT generalize or reinterpret rules

Each rule in the input MUST appear exactly once in the output.
Use:
* "id" = EXACT same as input
* "text" = EXACT rule description from input

---
# 🚨 NO NEW RULES
If a rule is not present in the input, it MUST NOT appear in the output.
The output must be a direct classification of the given rules ONLY.

---
# 🚨 IMPORTANT CONTEXT
The input rules contain ONLY eligibility conditions such as:
* turnover requirements
* experience requirements
* performance criteria

Policy, compliance, and regulatory conditions are NOT part of this task.

---
# 🚨 CRITICAL RELEVANCE RULE
Data must be **logically relevant to the rule**, not just loosely related.

### ❌ DO NOT use:
* PAN, GST, CIN, bank account, or identity fields

### ✅ VALID USAGE:
* turnover → revenue / income
* experience → years / projects / past work

If data does NOT directly support evaluation → mark as NON_CHECKABLE.

---
### 🚫 STRICT DOMAIN GUARD (VERY IMPORTANT)

DO NOT approximate or substitute data across categories.

Examples:

❌ DO NOT use:
- company establishment year → for experience
- company age → for project experience
- revenue → for net worth
- identity data → for eligibility checks

### EXPERIENCE RULES (STRICT)

Valid experience data MUST include:
- past projects
- work orders
- contracts
- service history

Company age or incorporation year is NOT valid experience.

If only company age exists → mark as NON_CHECKABLE.

---
# 🔁 SCHEMA-AGNOSTIC UNDERSTANDING
* Field names may vary
* Search the entire JSON
* Match based on semantic meaning (not exact keys)

---
# 🧩 SEMANTIC MAPPING
### Financial Rules
Look for:
* revenue
* turnover
* income

### Experience Rules
Look for:
* experience
* projects
* past work
* contracts

---
# ⚙️ CLASSIFICATION LOGIC
For EACH rule:
1. Read the rule EXACTLY as provided
2. Identify its concept (financial / experience / performance)
3. Search for relevant supporting data in customer_profile
4. If relevant data exists → checkable
5. If no relevant data exists → non_checkable

---
# ⚠️ IMPORTANT
* Checkable ≠ Pass
* Partial but relevant data is VALID
* Ignore unrelated fields
* DO NOT use prior knowledge of typical tender conditions

---
# 🧾 FIELD REPORTING RULES



## For checkable rules:
* "used_fields" MUST contain ONLY:
  * fields that actually exist
  * fields that are directly relevant

## For non_checkable rules:
* "missing_fields" MUST be human-readable (NOT technical paths)
Examples:
✔ "experience data not available"
✔ "financial data not available"

---

### 🚫 FIELD VALIDATION RULE

"used_fields" MUST reference ONLY fields that EXACTLY EXIST in the input customer_profile.

DO NOT:
- create new field paths
- rename fields
- generalize structure

❌ "financials.annual_turnover"
✔ "[4].data.totalRevenue"

If exact fields cannot be identified → mark as NON_CHECKABLE.

---
# 🚫 STRICT RESTRICTIONS
* DO NOT evaluate pass/fail
* DO NOT hallucinate data
* DO NOT invent fields
* DO NOT create or modify rules

---
# 📦 OUTPUT FORMAT (STRICT — DO NOT CHANGE)
{{
  "checkable_rules": [
    {{
      "id": "<rule id>",
      "text": "<exact rule description>",
      "used_fields": ["<actual.dotted.path>", ...]
    }}
  ],
  "non_checkable_rules": [
    {{
      "id": "<rule id>",
      "text": "<exact rule description>",
      "missing_fields": ["<semantic missing data description>"]
       "how_to_make_checkable": "<actionable instruction>"
    }}
  ]
}}

---
# 🔒 HARD CONSTRAINTS
* Return ONLY valid JSON
* Follow the structure EXACTLY as shown above
* DO NOT add extra fields
* DO NOT remove any keys
* DO NOT include explanations
* Every rule must appear exactly once

---
# 🎯 GOAL
Produce a strict, deterministic classification of the given rules without altering or inventing any rule.

---
# 📥 INPUT DATA TO CLASSIFY

## TENDER RULES:
{rules_json}

## VENDOR PROFILE:
{customer_profile_json}
"""


# ────────────────────────────────────────────────────────
# Rule Evaluation Prompt — Agent 4 (v1.0)
# ────────────────────────────────────────────────────────

RULE_EVALUATION_PROMPT = """\
You are a deterministic rule evaluation engine for Indian GeM procurement auditing.

You receive:

1. A list of CHECKABLE eligibility rules.
2. The customer_profile JSON.

Your task is to evaluate each rule and determine whether the vendor PASSES or FAILS.

---

# 🧠 CORE PRINCIPLE

Evaluate rules using ONLY:

* The rule description
* Data explicitly present in customer_profile
A rule is checkable ONLY if sufficient data exists to conclusively evaluate it.

If evaluation would require assumptions → mark as non_checkable.

DO NOT:

* Infer missing data
* Use external knowledge
* Reinterpret rule meaning

---

# ⚙️ RULE TYPE HANDLING (MANDATORY)

You MUST interpret rules correctly based on their intent:

---

## 1. Eligibility Rules (numeric / threshold)

Examples:

* turnover ≥ X
* experience ≥ Y

👉 Action:

* Compare actual vs required

✔ PASS if condition satisfied
❌ FAIL if condition not satisfied

---

## 2. Certificate / Document Rules

Examples:

* turnover_proof required
* experience_proof required
* mandatory documents list

👉 Action:

* Check presence in `documents_available`

✔ PASS if present
❌ FAIL if missing

---

## 3. Exemption Rules (CRITICAL)

Examples:

* "MSE exemption is not provided"
* "Startup exemption is not provided"

👉 Interpretation:

These are POLICY rules, NOT eligibility checks.

👉 If rule says exemption is NOT allowed:

* Vendor automatically PASSES this rule
* DO NOT check msme or startup status
* DO NOT reverse the logic

---

## 4. Regulatory Rules

Examples:

* land border country restriction

👉 Action:

* Evaluate ONLY using available data
* If sufficient data proves violation → FAIL
* If data is insufficient → PASS

⚠️ NEVER:

* infer country from GSTIN
* assume external mappings

---

## 5. Conditional / Derived Rules (EDGE CASE)

If a rule requires:

* derived values (e.g., 3-year average)
* multi-step logic
* missing required fields

👉 Action:

* Use ONLY available data
* If evaluation cannot be reliably performed:
  ✔ PASS with evidence: "Insufficient data to disprove compliance"

DO NOT guess or fabricate values

---

# 🧾 EVALUATION STEPS

For EACH rule:

1. Identify relevant fields from customer_profile
2. Apply correct rule type logic
3. Perform factual comparison
4. Decide PASS or FAIL
5. Provide evidence using actual values

---

# 🧾 EVIDENCE REQUIREMENTS

Evidence MUST:

* Reference actual field paths
* Include actual values

Examples:

* "experience.years = 0, required ≥ 1"
* "documents_available does not contain 'experience_proof'"

---

# 🚫 STRICT RESTRICTIONS

* DO NOT hallucinate data
* DO NOT infer missing values
* DO NOT create new fields
* DO NOT reinterpret rules
* DO NOT use external knowledge

---

# 📦 OUTPUT FORMAT (STRICT)

Return ONLY valid JSON:

{{
"passed": [
{{
"rule_id": "<rule id>",
"evidence": "<factual evidence using actual data>"
}}
],
"failed": [
{{
"rule_id": "<rule id>",
"reason": "<clear factual comparison or missing requirement>",
"evidence": "<actual values from customer_profile>"
}}
]
}}

---
For each rule, ALWAYS include:
- "rule_text": the original rule description

Evidence MUST:
- Explain what the rule means
- Explain why it passed or failed
- Reference actual customer data

Do NOT give generic explanations.
Make output audit-friendly and self-explanatory.

# 🔒 HARD CONSTRAINTS

* EVERY rule must appear exactly once
* DO NOT skip any rule
* DO NOT add extra text
* Output must be valid JSON
* Output must be deterministic

---

# INPUTS

### Checkable Rules:

{checkable_rules_json}

### Customer Profile:

{customer_profile_json}

"""


# ────────────────────────────────────────────────────────
# Verifiable Eligibility Filter Prompt — Agent 5 (v1.0)
# ────────────────────────────────────────────────────────

VERIFIABLE_FILTER_PROMPT = """\
You are Agent 5: Verifiable Eligibility Filter for a tender analysis system.

You receive a JSON object containing extracted eligibility criteria from Agent 1.

Your task is to classify each rule as VERIFIABLE or NOT VERIFIABLE before bid submission.

---

# DEFINITION

A rule is considered relevant only if it is BOTH:
1. Verifiable using documents or factual data
2. Defines bidder eligibility or qualification BEFORE bid submission

Do not include rules that are only about:
- document formats
- submission procedures
- actions required during bidding

If a rule requires submission of a document that proves an eligibility condition
(e.g., certificate, registration, experience proof),
then it should be considered verifiable eligibility.

Only exclude rules where the document is purely procedural
(e.g., format, annexure template, submission method).

If a certification is required (even with future clause),
treat it as verifiable eligibility.


A rule is NOT VERIFIABLE if:

* It applies after bid submission or after contract award
* It is a bidding process rule (reverse auction, pricing rules, bid extension)
* It is a general compliance or policy statement
* It describes actions during execution of contract
* It is an informational or procedural statement

---

# OUTPUT FORMAT (STRICT JSON)

{{
  "verifiable_criteria": [
    {{
      "id": "<original id>",
      "text": "<original text>",
      "summary": "<original summary>",
      "reason": "short 1-line reason why this is verifiable"
    }}
  ],
  "non_verifiable_criteria": [
    {{
      "id": "<original id>",
      "text": "<original text>",
      "summary": "<original summary>",
      "reason": "short 1-line reason why this is not verifiable"
    }}
  ]
}}

---

# RULES

* Do NOT modify original text, id, or summary — carry them forward exactly
* Add a short reason (1 line max)
* Be strict but practical
* Every input criterion must appear exactly once in the output
* Do not hallucinate or invent criteria
* Return ONLY valid JSON — no markdown fences

---

# INPUT

{eligibility_criteria_json}

"""

