"""
Pydantic models for the GeM Procurement Audit Service — Schema v2.0


"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ────────────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────────────

class RequirementClarity(str, Enum):
    """How clearly the bid document states this requirement."""
    CLEAR = "CLEAR"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class ComplianceStatus(str, Enum):
    """Vendor compliance against a criterion.
    UNKNOWN = bid-extraction only (no vendor data yet).
    """
    UNKNOWN = "UNKNOWN"
    MET = "MET"
    NOT_MET = "NOT_MET"
    PARTIAL = "PARTIAL"


class ComparisonOperator(str, Enum):
    """Machine-evaluable comparisons for structured required_value."""
    GTE = ">="
    LTE = "<="
    EQ = "=="
    GT = ">"
    LT = "<"
    IN = "IN"
    CONTAINS = "CONTAINS"
    BOOLEAN = "BOOLEAN"
    BETWEEN = "BETWEEN"


class RiskCategory(str, Enum):
    """Taxonomy separating systemic GeM boilerplate from actual risks."""
    SYSTEMIC_GEM_RISK = "SYSTEMIC_GEM_RISK"
    BUYER_ATC_RISK = "BUYER_ATC_RISK"
    BID_SPECIFIC_COMPLIANCE_RISK = "BID_SPECIFIC_COMPLIANCE_RISK"
    VENDOR_DOCUMENT_RISK = "VENDOR_DOCUMENT_RISK"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# ────────────────────────────────────────────────────────
# Shared sub-models
# ────────────────────────────────────────────────────────

class BoundingBox(BaseModel):
    """Normalised 0–1000 bounding box for UI highlight overlay."""
    ymin: float = Field(..., ge=0, le=1000)
    xmin: float = Field(..., ge=0, le=1000)
    ymax: float = Field(..., ge=0, le=1000)
    xmax: float = Field(..., ge=0, le=1000)


class DocumentReference(BaseModel):
    """Reusable pointer to a specific location in a source PDF."""
    reference_id: Optional[str] = Field(
        None,
        description="Unique ID (e.g. 'ref-001') for deduplication and cross-criterion reuse",
    )
    filename: str
    page: Optional[int] = Field(None, ge=1)
    section: Optional[str] = None
    clause: Optional[str] = Field(None, description="Clause number, e.g. 'ATC-4.2'")
    bounding_box: Optional[BoundingBox] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


class StructuredRequirement(BaseModel):
    """Machine-evaluable requirement — replaces free-text required_value."""
    comparison_operator: Optional[ComparisonOperator] = Field(
        None, description="How to compare: >=, ==, IN, BOOLEAN, etc."
    )
    numeric_value: Optional[float] = Field(None, description="Threshold number if applicable")
    unit: Optional[str] = Field(
        None,
        description="Unit: INR, years, percentage, count, boolean, enum, etc.",
    )
    text_value: Optional[str] = Field(
        None,
        description="For IN/BOOLEAN/enum: the allowed values or description",
    )
    raw_text: Optional[str] = Field(
        None,
        description="Original text from the document, verbatim",
    )

    @field_validator("comparison_operator", mode="before")
    @classmethod
    def coerce_comparison_operator(cls, v):
        """Accept unknown operators from Gemini without crashing."""
        if v is None:
            return None
        try:
            return ComparisonOperator(v)
        except ValueError:
            # Map common unknown values to closest valid operator
            mapping = {
                "NOT_IN": "IN",
                "MATCHES": "CONTAINS",
                "LIKE": "CONTAINS",
                "NOT_EQUAL": "==",
                "!=": "==",
            }
            mapped = mapping.get(str(v).upper())
            if mapped:
                return ComparisonOperator(mapped)
            # Fallback: treat as equality check
            return ComparisonOperator.EQ

    @field_validator("numeric_value", mode="before")
    @classmethod
    def coerce_numeric_value(cls, v):
        if isinstance(v, list):
            return v[0] if v else None
        return v


class EligibilityCriterion(BaseModel):
    """A single eligibility criterion — v2 with split status semantics."""
    criterion_id: Optional[str] = Field(
        None, description="Machine-friendly ID, e.g. 'FINANCIAL_TURNOVER_BIDDER'"
    )
    criterion: str = Field(..., description="Human-readable criterion name")

    # ── Split status model ──
    bid_requirement_clarity: RequirementClarity = Field(
        default=RequirementClarity.CLEAR,
        description="How clearly the bid document states this requirement",
    )
    vendor_compliance_status: ComplianceStatus = Field(
        default=ComplianceStatus.UNKNOWN,
        description="Vendor compliance. UNKNOWN during bid extraction (no vendor data yet)",
    )

    detail: str = Field(default="", description="Explanation / audit narrative")
    extracted_value: Optional[str] = Field(None, description="Value found in vendor docs (Stage 2)")

    @field_validator("extracted_value", mode="before")
    @classmethod
    def coerce_extracted_value(cls, v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return v

    required_value: Optional[StructuredRequirement] = Field(
        None, description="Machine-evaluable requirement from the bid"
    )
    required_value_raw: Optional[str] = Field(
        None, description="Original free-text requirement as stated in document"
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    risk_level: Optional[Severity] = None
    risk_reasoning: Optional[str] = None
    human_readable_requirement: Optional[str] = Field(
        None,
        description="Plain-English explanation of the requirement, suitable for frontend display",
    )
    references: List[DocumentReference] = Field(default_factory=list)


class Relaxation(BaseModel):
    """A preference / relaxation (MSE, Startup, MII, etc.)"""
    criterion_id: Optional[str] = Field(None, description="e.g. 'MSE_PREFERENCE'")
    criterion: str = Field(..., description="Human-readable name")
    is_applicable: Optional[bool] = Field(
        None, description="Whether the bid enables this relaxation (Yes/No in document)"
    )
    vendor_compliance_status: ComplianceStatus = Field(
        default=ComplianceStatus.UNKNOWN,
        description="Vendor eligibility for this relaxation",
    )
    detail: str = Field(default="")
    extracted_value: Optional[str] = None

    @field_validator("extracted_value", mode="before")
    @classmethod
    def coerce_extracted_value(cls, v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return v

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    references: List[DocumentReference] = Field(default_factory=list)


class RiskFlag(BaseModel):
    """A categorised risk with taxonomy."""
    risk_id: Optional[str] = Field(None, description="Unique risk ID, e.g. 'RISK-001'")
    category: RiskCategory = Field(
        default=RiskCategory.BID_SPECIFIC_COMPLIANCE_RISK,
        description="Risk taxonomy bucket",
    )
    severity: Severity = Field(default=Severity.MEDIUM)
    title: Optional[str] = Field(None, description="Short risk title")
    description: str = Field(default="")
    recommendation: str = Field(default="")
    affected_criteria: List[str] = Field(
        default_factory=list,
        description="criterion_id list this risk impacts",
    )
    references: List[DocumentReference] = Field(default_factory=list)


class NormalizationMeta(BaseModel):
    """Token / usage accounting."""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    model: Optional[str] = None
    processing_time_seconds: Optional[float] = None


# ────────────────────────────────────────────────────────
# Stage 1 — Bid Analysis Response (v2.0)
# ────────────────────────────────────────────────────────

class EMDDetails(BaseModel):
    amount: Optional[str] = None
    currency: Optional[str] = Field(default="INR")
    bank: Optional[str] = None
    beneficiary: Optional[str] = None
    exemption_available: Optional[bool] = Field(
        None, description="Whether MSE/Startup exemption is noted"
    )
    references: List[DocumentReference] = Field(default_factory=list)


class DeliveryItem(BaseModel):
    """Flattened, DB-friendly scope of work line item."""
    item_code: Optional[str] = Field(None, description="GeM item/service code")
    item_name: Optional[str] = None
    description: Optional[str] = None
    consignee: Optional[str] = Field(None, description="Delivery location / consignee name")
    quantity: Optional[float] = None
    unit: Optional[str] = Field(None, description="e.g. 'pages', 'units', 'lots'")
    delivery_days: Optional[int] = Field(None, description="Delivery period in days")
    delivery_window: Optional[str] = Field(
        None, description="Start–end date range if specified"
    )
    references: List[DocumentReference] = Field(default_factory=list)


class ScopeOfWork(BaseModel):
    technical_specs: Optional[Dict[str, Any]] = Field(default_factory=dict)
    delivery_items: List[DeliveryItem] = Field(
        default_factory=list,
        description="Flattened line items — replaces nested timelines.details[]",
    )
    timelines: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Overall timeline metadata (contract period, milestones)",
    )
    references: List[DocumentReference] = Field(default_factory=list)


class SimilarServicesRule(BaseModel):
    """Tiered similar-service qualification option."""
    option_label: str = Field(..., description="e.g. '3 projects @ 40%'")
    min_projects: Optional[int] = None
    min_percentage_of_bid: Optional[float] = None
    references: List[DocumentReference] = Field(default_factory=list)


class BidAnalysisResponse(BaseModel):
    """POST /analyze-bid response — Schema v2.0

    Key order:
      schema_version → source → bid_id → metadata →
      eligibility_criteria → emd → scope_of_work → relaxations →
      similar_services_rules → risks → normalization_meta → raw_ocr_text
    """

    schema_version: str = Field(default="2.0.0")
    source: Optional[str] = Field(default=None)
    bid_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def _coerce_metadata(cls, v):
        return v if v is not None else {}


    @field_validator("similar_services_rules", "relaxations", "risks", "eligibility_criteria", mode="before")
    @classmethod
    def _coerce_list_fields(cls, v):
        return v if v is not None else []

    eligibility_criteria: List[EligibilityCriterion] = Field(default_factory=list)
    emd: Optional[EMDDetails] = None
    scope_of_work: Optional[ScopeOfWork] = None
    relaxations: Optional[List[Relaxation]] = Field(default_factory=list)
    similar_services_rules: Optional[List[SimilarServicesRule]] = Field(default_factory=list)
    risks: Optional[List[RiskFlag]] = Field(default_factory=list)

    normalization_meta: Optional[NormalizationMeta] = None
    raw_ocr_text: Optional[str] = Field(
        None, description="Full OCR text dump — MUST be the last key",
    )


# ────────────────────────────────────────────────────────
# Stage 2 — Vendor Evaluation Response (v2.0)
# ────────────────────────────────────────────────────────

class VendorProfile(BaseModel):
    name: Optional[str] = None
    pan: Optional[str] = None
    gst: Optional[str] = None
    address: Optional[str] = None
    registration_state: Optional[str] = None
    mse_status: Optional[bool] = None
    startup_status: Optional[bool] = None
    mse_certificate_valid_until: Optional[str] = None
    startup_certificate_valid_until: Optional[str] = None


class VendorEvaluationResponse(BaseModel):
    """POST /evaluate-vendor response — Schema v2.0

    Key order:
      schema_version → source → bid_id → metadata →
      vendor_profile → eligibility_score →
      financial_turnover → experience → similar_services →
      location_verification → relaxations → risks →
      overall_recommendation → rejection_reasons →
      normalization_meta → raw_ocr_text
    """

    schema_version: str = Field(default="2.0.0")
    source: Optional[str] = None
    bid_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def _coerce_metadata(cls, v):
        return v if v is not None else {}

    vendor_profile: Optional[VendorProfile] = None

    eligibility_score: float = Field(
        default=0.0, ge=0, le=100,
        description="Composite eligibility score 0-100",
    )

    # Criterion-wise results (each is a full EligibilityCriterion)
    financial_turnover: Optional[EligibilityCriterion] = None
    experience: Optional[EligibilityCriterion] = None
    similar_services: Optional[EligibilityCriterion] = None
    location_verification: Optional[EligibilityCriterion] = None

    relaxations: Optional[List[Relaxation]] = Field(default_factory=list)
    risks: Optional[List[RiskFlag]] = Field(default_factory=list)

    overall_recommendation: Optional[str] = Field(
        None, description="APPROVE / REJECT"
    )
    rejection_reasons: List[str] = Field(
        default_factory=list,
        description="Audit-grade reasons if REJECT — deterministic from criteria",
    )
    acceptance_reasons: List[str] = Field(
        default_factory=list,
        description="Justification if APPROVE based on >= 60 score",
    )

    normalization_meta: Optional[NormalizationMeta] = None
    raw_ocr_text: Optional[str] = None


# ────────────────────────────────────────────────────────
# Orchestration — /process-bid-evaluation (v2.0)
# ────────────────────────────────────────────────────────

class VendorInput(BaseModel):
    """A single vendor with document URLs, as received from Pub/Sub or backend."""
    vendor_id: str = Field(..., description="Unique vendor identifier")
    documents: List[str] = Field(
        ...,
        description="List of S3 URLs to vendor PDFs (e.g. s3://bucket/vendor_01/gst.pdf)",
        min_length=1,
    )


class BidEvaluationRequest(BaseModel):
    """POST /process-bid-evaluation request body."""
    bid_id: str = Field(..., description="GeM Bid / Tender ID")
    bid_document_url: str = Field(
        ...,
        description="S3 URL to the bid PDF (e.g. s3://bids/bid_6716709.pdf)",
    )
    vendors: List[VendorInput] = Field(
        ...,
        description="List of vendors to evaluate against the bid",
        min_length=1,
    )


class VendorEvaluationSummary(BaseModel):
    """Per-vendor result in the orchestration response."""
    vendor_id: str
    eligibility_score: float = Field(default=0.0, ge=0, le=100)
    recommendation: Optional[str] = Field(
        None, description="APPROVE / REJECT"
    )
    criterion_verdicts: List[EligibilityCriterion] = Field(default_factory=list)
    vendor_profile: Optional[VendorProfile] = None
    rejection_reasons: List[str] = Field(default_factory=list)
    acceptance_reasons: List[str] = Field(default_factory=list)
    risks: Optional[List[RiskFlag]] = Field(default_factory=list)
    error: Optional[str] = Field(
        None, description="Error message if this vendor's evaluation failed"
    )


class BidEvaluationResponse(BaseModel):
    """POST /process-bid-evaluation response."""
    bid_id: str
    bid_analysis: Optional[BidAnalysisResponse] = None
    vendor_evaluations: List[VendorEvaluationSummary] = Field(default_factory=list)
    summary: str = Field(
        default="",
        description="Human-readable summary of the overall evaluation",
    )
    errors: List[str] = Field(
        default_factory=list,
        description="Top-level errors encountered during processing",
    )


# ────────────────────────────────────────────────────────
# Stage 3 — Bid Submission Package (v1.0)
# ────────────────────────────────────────────────────────

class DocumentType(str, Enum):
    """Category of a document within a bid submission package."""
    STANDARD = "STANDARD"
    DECLARATION = "DECLARATION"
    GENERATED = "GENERATED"
    ANNEXURE = "ANNEXURE"


class DocumentSource(str, Enum):
    """Origin of a document in the submission package."""
    EXISTING = "existing"
    GENERATED = "generated"
    TEMPLATE = "template"
    MISSING = "missing"


class DocumentMetadata(BaseModel):
    """Metadata for a single document in the bid submission package."""
    name: str = Field(..., description="Document filename or identifier")
    type: DocumentType = Field(..., description="STANDARD | DECLARATION | GENERATED | ANNEXURE")
    source: DocumentSource = Field(..., description="existing | generated | template | missing")
    description: str = Field(default="", description="Brief description of the document")


class BidSubmissionPackageRequest(BaseModel):
    """POST /generate-bid-package request body."""
    bid_analysis: BidAnalysisResponse = Field(
        ..., description="Structured output from Stage 1 bid analysis"
    )
    vendor_evaluation: VendorEvaluationResponse = Field(
        ..., description="Structured output from Stage 2 vendor evaluation"
    )
    vendor_documents: List[str] = Field(
        default_factory=list,
        description="List of vendor document filenames or identifiers (e.g. 'pan.pdf', 'gst_certificate.pdf')",
    )


class BidSubmissionPackageResponse(BaseModel):
    """POST /generate-bid-package response."""
    status: str = Field(..., description="SUCCESS or REJECTED")
    message: Optional[str] = Field(
        None, description="Human-readable status message"
    )
    documents: List[DocumentMetadata] = Field(
        default_factory=list,
        description="Complete list of documents in the submission package",
    )
    generated_sections: Optional[Dict[str, Any]] = Field(
        None,
        description="AI-generated document sections (only for missing generatable docs)",
    )
    gap_analysis: Optional[Dict[str, Any]] = Field(
        None,
        description="Gap analysis: required, present, missing, generatable, not_generatable",
    )


class PDFGenerationRequest(BaseModel):
    companyId: str
    customerId: str
    bid_analysis: BidAnalysisResponse
    vendor_evaluation: VendorEvaluationSummary
    docsLink: List[str]

class PDFGenerationResponse(BaseModel):
    companyId: str
    customerId: str
    status: str
    pdf_url: Optional[str] = None
    error: Optional[str] = None

