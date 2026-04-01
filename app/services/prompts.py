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
