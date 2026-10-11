import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    raw_text: str = Field(min_length=8, max_length=20000)


class ExtractedLoanData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicant_name_english: str = Field(max_length=150)
    applicant_name_marathi: str = Field(max_length=150)
    bank_or_society_name: str = Field(max_length=200)
    account_number: str = Field(max_length=50)
    pan_number: str | None = None
    monthly_income: float = Field(ge=0, le=100000000)
    district: str = Field(default="", max_length=100)

    @field_validator("pan_number")
    @classmethod
    def validate_pan(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        cleaned = value.strip().upper()
        if re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", cleaned) is None:
            raise ValueError("PAN must match the Indian format ABCDE1234F")
        return cleaned


class LoanAssessment(ExtractedLoanData):
    eligibility_score: int = Field(ge=0, le=100)
    status: Literal["AUTO_APPROVED", "MANUAL_REVIEW", "REJECTED"]
    risk_flags: list[str] = Field(default_factory=list)
