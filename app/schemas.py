"""
Pydantic models for the GeM Procurement Audit Service — Schema v2.0


"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ────────────────────────────────────────────────────────
# Tender Rule Extraction — Agent 1 (v1.0)
# ────────────────────────────────────────────────────────

class ExtractedRule(BaseModel):
    """A single eligibility rule extracted from a tender document."""
    id: str = Field(..., description="Unique rule identifier, e.g. 'EC_1'")
    text: str = Field(..., description="Exact extracted eligibility statement")
    summary: Optional[str] = Field(None, description="Short human-readable explanation")


class TenderExtractionResult(BaseModel):
    """Complete result of tender rule extraction (Agent 1 output)."""
    tender_id: str = Field(default="UNKNOWN", description="Tender / Bid identifier")
    rules: List[ExtractedRule] = Field(default_factory=list, description="Extracted eligibility rules")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Extraction metadata (title, dates, etc.)")
    raw_ocr_text: Optional[str] = Field(None, description="Full OCR text from tender PDF")

    @field_validator("rules", mode="before")
    @classmethod
    def coerce_rules(cls, v):
        return v if v is not None else []

    @field_validator("metadata", mode="before")
    @classmethod
    def coerce_metadata(cls, v):
        return v if v is not None else {}



# ────────────────────────────────────────────────────────
# Verifiable Eligibility Filter — Agent 5 (v1.0)
# ────────────────────────────────────────────────────────

class VerifiableCriterion(BaseModel):
    """A criterion classified as verifiable or non-verifiable."""
    id: str = Field(..., description="Original criterion ID, e.g. 'EC_1'")
    text: str = Field(..., description="Original extracted text")
    summary: Optional[str] = Field(None, description="Original short summary")
    reason: str = Field(..., description="Why this criterion is/isn't verifiable")


class FilterRulesRequest(BaseModel):
    """POST /test/filter-rules request body.

    Accepts either:
    - The direct output of /test/extract-rules  (has a `rules` field)
    - A plain object with `eligibility_criteria`

    Extra fields (tender_id, metadata, raw_ocr_text) are silently ignored.
    """
    model_config = {"extra": "ignore"}

    eligibility_criteria: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of criteria from Agent 1 (as eligibility_criteria)"
    )
    rules: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Alias accepted when pasting extract-rules output directly"
    )

    @model_validator(mode="after")
    def merge_rules_into_criteria(self) -> "FilterRulesRequest":
        """If eligibility_criteria is empty but rules is populated, use rules."""
        if not self.eligibility_criteria and self.rules:
            self.eligibility_criteria = self.rules
        return self

    def get_criteria(self) -> List[Dict[str, Any]]:
        """Return the resolved criteria list (eligibility_criteria takes precedence)."""
        return self.eligibility_criteria or self.rules


class FilterRulesResponse(BaseModel):
    """POST /test/filter-rules response."""
    verifiable_criteria: List[VerifiableCriterion] = Field(default_factory=list)
    non_verifiable_criteria: List[VerifiableCriterion] = Field(default_factory=list)



# ────────────────────────────────────────────────────────
# Tender Analysis Result — Agent 2 (v1.0)
# ────────────────────────────────────────────────────────

class SummaryPoint(BaseModel):
    text: str
    importance: str = Field(description="high | medium | low")

class SummarySection(BaseModel):
    title: str
    type: str = Field(description="overview | requirements | commercial | dates | evaluation | scope | risk | other")
    points: List[SummaryPoint] = Field(default_factory=list)

class TenderAnalysisData(BaseModel):
    summary: str = Field(default="")
    highlights: List[str] = Field(default_factory=list)
    sections: List[SummarySection] = Field(default_factory=list)

class TenderAnalysisEndpointResponse(BaseModel):
    tender_analysis: TenderAnalysisData

class TenderAnalysisResult(BaseModel):
    """Result of tender analysis (Agent 2 output)."""
    tender_id: str = Field(default="UNKNOWN", description="Tender / Bid identifier")
    tender_analysis: TenderAnalysisData = Field(default_factory=TenderAnalysisData)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Metadata such as published date, title, etc.")

    @field_validator("metadata", mode="before")
    @classmethod
    def coerce_metadata_analysis(cls, v):
        return v if v is not None else {}


# ────────────────────────────────────────────────────────
# Test / Debug Endpoint Schemas
# ────────────────────────────────────────────────────────

class EmbeddedLinkOcr(BaseModel):
    sourceUrl: str
    ocrText: str

class ExtractRulesTestRequest(BaseModel):
    """POST /test/extract-rules request body."""
    bidOcr: str = Field(
        ..., description="Raw OCR text of the bid"
    )
    embeddedLinkOcr: List[EmbeddedLinkOcr] = Field(
        default_factory=list, description="List of embedded links and their extracted OCR text"
    )


class AnalyzeBidTestRequest(BaseModel):
    """POST /test/analyze-bid request body."""
    tender_document: str = Field(
        ..., description="Raw text content of the tender document"
    )


# ── Classification Agent schemas ─────────────────────────

class CheckableRule(BaseModel):
    """A rule that CAN be evaluated against the customer profile."""
    
    id: str = Field(..., description="Rule identifier, e.g. 'rule_1'")
    text: str = Field(..., description="Human-readable rule text")
    
    used_fields: List[str] = Field(
        default_factory=list,
        description="Customer profile fields used to check this rule",
    )


class NonCheckableRule(BaseModel):
    id: str = Field(...)
    text: str = Field(...)
    missing_fields: List[str] = Field(...)
    how_to_make_checkable: str = Field(
        default="Provide required information manually or upload supporting documents",
        description="Instructions for the user"
    )


class ClassifyRulesRequest(BaseModel):
    """POST /test/classify-rules request body."""
    
    rules: List[Dict[str, Any]] = Field(
        ..., description="List of extracted rules to classify"
    )
    
    # ✅ FIX: Accept both dict and list
    customer_profile: Union[Dict[str, Any], List[Any]] = Field(
        ..., description="Structured customer profile (dict or list)"
    )


class ClassifyRulesResponse(BaseModel):
    """POST /test/classify-rules response."""
    
    checkable_rules: List[CheckableRule] = Field(default_factory=list)
    non_checkable_rules: List[NonCheckableRule] = Field(default_factory=list)

# ── Evaluation Agent schemas ─────────────────────────────

class PassedRule(BaseModel):
    """A rule the customer passed."""
    rule_id: str = Field(..., description="Rule identifier")
    evidence: str = Field(..., description="Evidence from customer data supporting the pass")


class FailedRule(BaseModel):
    """A rule the customer failed."""
    rule_id: str = Field(..., description="Rule identifier")
    reason: str = Field(..., description="Why the rule was not met")
    evidence: str = Field(..., description="Specific customer data that caused the failure")


class EvaluateRulesRequest(BaseModel):
    """POST /test/evaluate-rules request body."""
    checkable_rules: List[Dict[str, Any]] = Field(
        ..., description="List of checkable rules (output from classification)"
    )
    
    # ✅ FIXED: Changed to Union to accept both dict and list arrays
    customer_profile: Union[Dict[str, Any], List[Any]] = Field(
        ..., description="Structured customer profile from NestJS (dict or list)"
    )


class EvaluateRulesResponse(BaseModel):
    """POST /test/evaluate-rules response."""
    passed: List[PassedRule] = Field(default_factory=list)
    failed: List[FailedRule] = Field(default_factory=list)

# ── Full Eligibility schemas ────────────────────────────

class FullEligibilityRequest(BaseModel):
    """POST /test/full-eligibility request body."""
    rules: List[Dict[str, Any]] = Field(
        ..., description="List of extracted rules"
    )
    customer_profile: Union[Dict[str, Any], List[Any]] = Field(
        ..., description="Structured customer profile from NestJS (can be Dict or List)"
    )


class FullEligibilityResponse(BaseModel):
    """POST /test/full-eligibility response."""
    classification: ClassifyRulesResponse
    evaluation: EvaluateRulesResponse

