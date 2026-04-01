"""
Document Classifier & Gap Analyzer for Bid Submission Packages.

Core logic:
  1. Extract REQUIRED documents from bid_analysis (eligibility criteria, EMD, etc.)
  2. Match vendor filenames against required categories → present vs missing
  3. Classify existing docs for the final package

No PDF re-processing is done — everything is filename / JSON based.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from app.schemas import (
    BidAnalysisResponse,
    DocumentMetadata,
    DocumentSource,
    DocumentType,
)


# ────────────────────────────────────────────────────────
# Required document categories
# ────────────────────────────────────────────────────────

# Standard docs: vendor MUST already have these (cannot be AI-generated)
_STANDARD_CATEGORIES: Dict[str, List[str]] = {
    "PAN": ["pan"],
    "GST": ["gst"],
    "CIN / Incorporation": ["cin", "incorporation"],
    "MSE / Udyam Certificate": ["mse", "udyam"],
    "Financial Statement": ["balance", "turnover", "financial", "p&l", "profit"],
    "Work Order / Experience": ["work_order", "workorder", "completion", "experience"],
    "Address Proof": ["address", "proof"],
    "DPIIT / Startup Certificate": ["dpiit", "startup"],
}

# Generatable docs: these CAN be produced by AI if missing
_GENERATABLE_CATEGORIES: Dict[str, List[str]] = {
    "cover_letter": ["cover_letter", "cover letter", "coverletter"],
    "technical_compliance": ["technical_compliance", "technical compliance", "tech_compliance"],
    "financial_compliance": ["financial_compliance", "financial compliance", "fin_compliance"],
    "experience_statement": ["experience_statement", "experience statement"],
    "declaration": ["undertaking", "declaration", "affidavit", "compliance_declaration"],
}


# ────────────────────────────────────────────────────────
# Data classes for gap analysis
# ────────────────────────────────────────────────────────

@dataclass
class GapAnalysisResult:
    """Result of comparing required vs available documents."""
    required: List[str] = field(default_factory=list)
    present: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    generatable: List[str] = field(default_factory=list)  # subset of missing that CAN be generated
    not_generatable: List[str] = field(default_factory=list)  # subset of missing that CANNOT be generated

    def to_dict(self) -> dict:
        return {
            "required": self.required,
            "present": self.present,
            "missing": self.missing,
            "generatable": self.generatable,
            "not_generatable": self.not_generatable,
        }


# ────────────────────────────────────────────────────────
# Step 1: Extract required documents from bid_analysis
# ────────────────────────────────────────────────────────

def extract_required_documents(bid_analysis: BidAnalysisResponse) -> List[str]:
    """Extract the list of required document categories from bid analysis.

    Reads eligibility_criteria, emd, relaxations, and scope_of_work to
    determine what documents are needed for submission.
    """
    required: Set[str] = set()

    # Always required for any GeM bid
    required.add("PAN")
    required.add("GST")
    required.add("cover_letter")
    required.add("declaration")

    # Check eligibility criteria for specific requirements
    for criterion in bid_analysis.eligibility_criteria:
        cid = (criterion.criterion_id or "").upper()
        name = (criterion.criterion or "").upper()

        if "FINANCIAL" in cid or "TURNOVER" in cid or "FINANCIAL" in name:
            required.add("Financial Statement")
            required.add("financial_compliance")

        if "EXPERIENCE" in cid or "EXPERIENCE" in name:
            required.add("Work Order / Experience")
            required.add("experience_statement")

        if "SIMILAR" in cid or "SIMILAR" in name:
            required.add("Work Order / Experience")
            required.add("experience_statement")

        if "LOCAL" in cid or "LOCATION" in cid or "OFFICE" in name:
            required.add("Address Proof")

        if "CERTIF" in cid or "REGISTRATION" in cid:
            required.add("CIN / Incorporation")

        if "TECHNICAL" in cid or "TECHNICAL" in name or "QUALIFICATION" in name:
            required.add("technical_compliance")

    # EMD may require financial docs
    if bid_analysis.emd and bid_analysis.emd.amount:
        required.add("Financial Statement")
        required.add("financial_compliance")

    # Relaxations may require MSE/DPIIT certs
    for relaxation in (bid_analysis.relaxations or []):
        rid = (relaxation.criterion_id or "").upper()
        if "MSE" in rid or "UDYAM" in rid:
            required.add("MSE / Udyam Certificate")
        if "STARTUP" in rid or "DPIIT" in rid:
            required.add("DPIIT / Startup Certificate")

    # Technical compliance is always useful if there are technical specs
    if bid_analysis.scope_of_work and bid_analysis.scope_of_work.technical_specs:
        required.add("technical_compliance")

    return sorted(required)


# ────────────────────────────────────────────────────────
# Step 2: Match vendor documents against requirements
# ────────────────────────────────────────────────────────

def _filename_matches_category(filename: str, category: str) -> bool:
    """Check if a vendor filename matches a required category."""
    lower = filename.lower()

    # Check standard categories
    if category in _STANDARD_CATEGORIES:
        return any(kw in lower for kw in _STANDARD_CATEGORIES[category])

    # Check generatable categories
    if category in _GENERATABLE_CATEGORIES:
        return any(kw in lower for kw in _GENERATABLE_CATEGORIES[category])

    return False


def get_missing_documents(
    required: List[str],
    vendor_documents: List[str],
) -> GapAnalysisResult:
    """Compare required documents against vendor's existing documents.

    Returns a GapAnalysisResult with present, missing, generatable, and
    not_generatable lists.
    """
    present: List[str] = []
    missing: List[str] = []

    for category in required:
        matched = any(
            _filename_matches_category(doc, category)
            for doc in vendor_documents
        )
        if matched:
            present.append(category)
        else:
            missing.append(category)

    # Split missing into generatable vs not-generatable
    generatable = [m for m in missing if m in _GENERATABLE_CATEGORIES]
    not_generatable = [m for m in missing if m not in _GENERATABLE_CATEGORIES]

    return GapAnalysisResult(
        required=required,
        present=present,
        missing=missing,
        generatable=generatable,
        not_generatable=not_generatable,
    )


# ────────────────────────────────────────────────────────
# Step 3: Classify existing vendor documents for output
# ────────────────────────────────────────────────────────

def classify_vendor_documents(filenames: List[str]) -> List[DocumentMetadata]:
    """Classify vendor document filenames into DocumentMetadata objects."""
    result: List[DocumentMetadata] = []

    for filename in filenames:
        lower = filename.lower()

        # Check if it's a declaration type
        declaration_kws = ["undertaking", "declaration", "affidavit"]
        if any(kw in lower for kw in declaration_kws):
            result.append(DocumentMetadata(
                name=filename,
                type=DocumentType.DECLARATION,
                source=DocumentSource.EXISTING,
                description=_describe(filename),
            ))
        else:
            result.append(DocumentMetadata(
                name=filename,
                type=DocumentType.STANDARD,
                source=DocumentSource.EXISTING,
                description=_describe(filename),
            ))

    return result


def _describe(filename: str) -> str:
    """Generate a short description based on filename keywords."""
    lower = filename.lower()
    descriptions = {
        "pan": "PAN Card",
        "gst": "GST Certificate",
        "cin": "Certificate of Incorporation / CIN",
        "incorporation": "Certificate of Incorporation / CIN",
        "mse": "MSE / Udyam Certificate",
        "udyam": "MSE / Udyam Certificate",
        "balance": "Financial Statement / Balance Sheet",
        "turnover": "Financial Turnover Statement",
        "work_order": "Work Order / Completion Certificate",
        "workorder": "Work Order / Completion Certificate",
        "completion": "Completion Certificate",
        "dpiit": "DPIIT Startup Certificate",
        "startup": "Startup Certificate",
        "undertaking": "Undertaking Document",
        "declaration": "Declaration Document",
        "affidavit": "Affidavit",
    }
    for keyword, desc in descriptions.items():
        if keyword in lower:
            return desc
    return "Vendor Document"
