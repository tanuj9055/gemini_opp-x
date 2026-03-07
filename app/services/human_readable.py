"""
Human-readable requirement generator.

Shared helper used by ``/analyze-bid``, ``/evaluate-vendor``, the
orchestrator, and the RabbitMQ worker to ensure every eligibility
criterion includes a clear English explanation for frontend display.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.logging_cfg import logger

_log = logger.getChild("human_readable")

# ────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────

_UNIT_SYMBOLS = {
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}

_OPERATOR_PHRASES = {
    ">=": "at least",
    ">":  "more than",
    "<=": "at most",
    "<":  "less than",
    "==": "exactly",
    "IN": "one of",
    "BOOLEAN": "",
    "BETWEEN": "between",
}

# Patterns that indicate the criterion is about document submission
_DOCUMENT_KEYWORDS = re.compile(
    r"additional\s+doc|document\s+submission|requested\s+in\s+atc|"
    r"upload.*certificate|submit.*document|attach.*proof",
    re.IGNORECASE,
)

# Criterion IDs / names that indicate local supplier content
_LOCAL_SUPPLIER_PATTERN = re.compile(
    r"local[_ ]supplier|local[_ ]content|make[_ ]in[_ ]india|class[_ ][12i]",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────
# Number formatting (Indian convention for INR)
# ────────────────────────────────────────────────────────

def _format_number(value: float, unit: Optional[str] = None) -> str:
    """Format a number with Indian convention for INR, or standard otherwise."""
    if unit and unit.upper() == "INR":
        if value >= 1_00_00_000:
            crores = value / 1_00_00_000
            if crores == int(crores):
                return f"₹{int(crores)} crore"
            return f"₹{crores:,.2f} crore"
        if value >= 1_00_000:
            lakhs = value / 1_00_000
            if lakhs == int(lakhs):
                return f"₹{int(lakhs)} lakh"
            return f"₹{lakhs:,.2f} lakh"
        if value == int(value):
            return f"₹{int(value):,}"
        return f"₹{value:,.2f}"

    if value == int(value):
        return str(int(value))
    return f"{value:,.2f}"


# ────────────────────────────────────────────────────────
# Bid-side: generate_human_readable_requirement
# ────────────────────────────────────────────────────────

def generate_human_readable_requirement(criterion: Dict[str, Any]) -> str:
    """Convert a structured eligibility criterion into a plain-English sentence.

    Handles:
      - NOT_FOUND / missing requirements
      - Document submission requirements (ATC docs)
      - Numeric thresholds (turnover, EMD, etc.)
      - Percentage / local-supplier class requirements
      - Boolean requirements
      - Set membership (IN) and range (BETWEEN) operators
      - Fallback to raw text or detail field
    """
    name: str = criterion.get("criterion", "This requirement")
    clarity = str(criterion.get("bid_requirement_clarity", "")).upper()
    criterion_id = str(criterion.get("criterion_id", "")).upper()
    rv = criterion.get("required_value")
    raw = criterion.get("required_value_raw", "") or ""
    detail = criterion.get("detail", "") or ""

    # ── 1. NOT_FOUND or missing requirement ──────────────────────
    if clarity == "NOT_FOUND":
        return (
            f"The bid document does not specify a requirement for {name.lower()}."
        )

    # ── 2. Document submission requirement ───────────────────────
    if _DOCUMENT_KEYWORDS.search(raw) or _DOCUMENT_KEYWORDS.search(detail):
        return _format_document_requirement(name, raw or detail)

    # ── 3. Structured required_value present ─────────────────────
    if rv and isinstance(rv, dict):
        operator = rv.get("comparison_operator")
        numeric = rv.get("numeric_value")
        unit = rv.get("unit", "") or ""
        text_val = rv.get("text_value")
        raw_text = rv.get("raw_text")

        # 3a. Percentage + local supplier pattern
        if unit.lower() == "percentage" and numeric is not None:
            if _LOCAL_SUPPLIER_PATTERN.search(criterion_id) or _LOCAL_SUPPLIER_PATTERN.search(name):
                cls = _detect_local_class(criterion_id, name, raw, detail)
                pct = int(numeric) if numeric == int(numeric) else numeric
                return (
                    f"Bidder must have at least {pct}% local content"
                    f" to qualify as a {cls} local supplier."
                )
            # Generic percentage
            phrase = _OPERATOR_PHRASES.get(operator, "at least")
            pct = int(numeric) if numeric == int(numeric) else numeric
            return f"The bidder must have {name.lower()} of {phrase} {pct}%."

        # 3b. BOOLEAN
        if operator == "BOOLEAN":
            if text_val and text_val.lower().strip() not in ("true", "false", "yes", "no"):
                return f"The bidder must {text_val.lower().rstrip('.')}."
            if text_val and text_val.lower().strip() in ("true", "yes"):
                return f"{name} is required for this bid."
            if text_val and text_val.lower().strip() in ("false", "no"):
                return f"{name} is not required for this bid."
            return f"The bidder must satisfy the {name.lower()} requirement."

        # 3c. IN (set membership)
        if operator == "IN":
            if text_val:
                return f"The bidder must be located in or operate from: {text_val}."
            return f"The bidder must meet the {name.lower()} requirement."

        # 3d. BETWEEN (range)
        if operator == "BETWEEN":
            if text_val:
                return f"The bidder must have {name.lower()} {text_val}."
            return f"The bidder must meet the {name.lower()} requirement."

        # 3e. Numeric comparison (>=, <=, ==, >, <)
        if operator and numeric is not None:
            phrase = _OPERATOR_PHRASES.get(operator, operator)
            formatted = _format_number(numeric, unit)
            unit_suffix = ""
            if unit and unit.upper() not in (
                "INR", "USD", "EUR", "GBP", "BOOLEAN", "ENUM", "PERCENTAGE",
            ):
                unit_suffix = f" {unit}"
            return (
                f"The bidder must have {name.lower()} of {phrase} "
                f"{formatted}{unit_suffix}."
            )

        # 3f. Fallback within structured value
        if raw_text:
            return f"{name}: {raw_text}"

    # ── 4. Fallback: use required_value_raw or detail ────────────
    if raw:
        # Check if it's a document-submission style raw value
        if _DOCUMENT_KEYWORDS.search(raw):
            return _format_document_requirement(name, raw)
        return f"{name}: {raw}"

    if detail:
        return f"{name}: {detail}"

    return f"{name}."


# ────────────────────────────────────────────────────────
# Vendor-side: generate_vendor_human_readable
# ────────────────────────────────────────────────────────

def generate_vendor_human_readable(criterion: Dict[str, Any]) -> str:
    """Generate a vendor-perspective explanation based on compliance status.

    For vendor evaluations, the human-readable text explains whether the
    vendor meets, fails, or lacks evidence for the requirement.
    """
    compliance = str(criterion.get("vendor_compliance_status", "")).upper()
    name = criterion.get("criterion", "this requirement")

    if compliance in ("MET", "COMPLIANT"):
        return (
            "The vendor satisfies this requirement based on the submitted documents."
        )
    if compliance in ("NOT_MET", "NON_COMPLIANT"):
        reasoning = criterion.get("risk_reasoning", "")
        if reasoning:
            return (
                f"The vendor does not meet this requirement. {reasoning.rstrip('.')}."
            )
        return (
            "The vendor does not meet this requirement based on the submitted documents."
        )
    if compliance == "PARTIAL":
        return (
            "The vendor partially meets this requirement. "
            "Manual review is recommended to confirm full compliance."
        )

    # UNKNOWN or any other value
    return (
        "The vendor documents do not provide enough information "
        "to determine compliance for this requirement."
    )


# ────────────────────────────────────────────────────────
# Injection helpers (used by endpoints & worker)
# ────────────────────────────────────────────────────────

def inject_human_readable_bid(criteria: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Populate ``human_readable_requirement`` on every bid criterion in-place.

    Always overwrites so the field is never ``null`` in the response.
    """
    for item in criteria:
        if isinstance(item, dict):
            item["human_readable_requirement"] = generate_human_readable_requirement(item)
    return criteria


def inject_human_readable_vendor(data: Dict[str, Any]) -> Dict[str, Any]:
    """Populate ``human_readable_requirement`` on vendor criterion-wise findings.

    Iterates over the standard criterion fields (financial_turnover, experience,
    similar_services, location_verification) as well as any list-style criterion
    verdicts.
    """
    # Named criterion fields
    for key in ("financial_turnover", "experience", "similar_services", "location_verification"):
        item = data.get(key)
        if isinstance(item, dict):
            item["human_readable_requirement"] = generate_vendor_human_readable(item)

    # List-style criterion_verdicts (used by orchestrator response)
    for item in data.get("criterion_verdicts", []):
        if isinstance(item, dict):
            item["human_readable_requirement"] = generate_vendor_human_readable(item)

    return data


# ────────────────────────────────────────────────────────
# Private helpers
# ────────────────────────────────────────────────────────

def _format_document_requirement(name: str, raw: str) -> str:
    """Format a document-submission requirement into a clean sentence."""
    # Try to extract individual document names from comma/semicolon separated text
    # e.g. "Additional Doc 1 (Requested in ATC), Additional Doc 2 (Requested in ATC)"
    docs = re.split(r"[,;]\s*", raw.strip())
    docs = [d.strip() for d in docs if d.strip()]

    if len(docs) == 1:
        return (
            f"The bidder must submit {docs[0].rstrip('.')} "
            f"as specified in the bid document."
        )
    if len(docs) >= 2:
        # "Document 1 and Document 2 as specified in the ATC section."
        formatted = ", ".join(docs[:-1]) + " and " + docs[-1]
        # Clean up parenthetical ATC references since we mention ATC at the end
        formatted = re.sub(r"\s*\(Requested in ATC\)", "", formatted, flags=re.IGNORECASE)
        return (
            f"The bidder must submit {formatted.rstrip('.')} "
            f"as specified in the ATC section."
        )

    return f"{name}: {raw}"


def _detect_local_class(criterion_id: str, name: str, raw: str, detail: str) -> str:
    """Detect the local supplier class (Class 1, Class 2, etc.)."""
    combined = f"{criterion_id} {name} {raw} {detail}"
    m = re.search(r"class[_ -]*([12iI])", combined, re.IGNORECASE)
    if m:
        cls = m.group(1).upper()
        if cls == "I":
            cls = "1"
        return f"Class {cls}"
    return "Class 1"
