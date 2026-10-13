"""FastAPI loan document extraction and assessment service."""

import json
import logging
import os
import re
from typing import Literal

from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("extraction_worker")

app = FastAPI(title="CoopLending AI Extraction Worker", version="1.0.0")

GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

if GROQ_KEY.startswith("gsk_"):
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_KEY)
    MODEL_NAME = "openai/gpt-oss-20b"
elif OPENAI_KEY and not OPENAI_KEY.startswith("dummy"):
    client = OpenAI(api_key=OPENAI_KEY)
    MODEL_NAME = "gpt-4o-mini"
else:
    client = None
    MODEL_NAME = "none"

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
PAN_PATTERN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")

FINANCIAL_KEYWORDS = (
    "बँक", "पतसंस्था", "खाते", "खातेदार", "वेतन", "उत्पन्न", "रुपये", "पॅन",
    "bank", "society", "patsanstha", "account", "acct", "salary", "income",
    "rs", "inr", "pan", "passbook", "loan"
)

SYSTEM_PROMPT = """You extract loan/passbook data for Maharashtra cooperative banks and Patsansthas.
Return ONLY a valid JSON object matching these exact keys:
{
  "bank_or_society_name": string,
  "applicant_name_marathi": string,
  "applicant_name_english": string,
  "account_number": string,
  "pan_number": string or null,
  "monthly_income": number,
  "district": string
}
Normalize Devanagari numerals (०-९) to Arabic digits (0-9). Transliterate Marathi names into standard English spellings. If PAN is missing, set "pan_number": null."""


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    raw_text: str = Field(min_length=8, max_length=20000)

    @field_validator("raw_text")
    @classmethod
    def validate_financial_content(cls, value: str) -> str:
        text_lower = value.lower()
        has_keyword = any(kw in text_lower for kw in FINANCIAL_KEYWORDS)
        has_numeric_sequence = bool(re.search(r"[0-9०-९]{4,}", value))

        if not (has_keyword or has_numeric_sequence):
            raise ValueError(
                "Document appears non-financial or unreadable: missing banking terminology or account/amount figures."
            )
        return value


class ExtractedLoanData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicant_name_english: str = Field(max_length=150)
    applicant_name_marathi: str = Field(max_length=150)
    bank_or_society_name: str = Field(max_length=200)
    account_number: str = Field(max_length=50)
    pan_number: str | None = None
    monthly_income: float = Field(default=0.0, ge=0, le=100000000)
    district: str = Field(default="", max_length=100)

    @field_validator("pan_number", mode="before")
    @classmethod
    def normalize_pan(cls, value: object) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip().upper()
        if not cleaned or cleaned.lower() in {"null", "none", "n/a", "na"}:
            return None
        return cleaned if PAN_PATTERN.fullmatch(cleaned) else None

    @field_validator("monthly_income", mode="before")
    @classmethod
    def coerce_income(cls, value: object) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.0
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class LoanAssessment(ExtractedLoanData):
    eligibility_score: int = Field(ge=0, le=100)
    status: Literal["AUTO_APPROVED", "MANUAL_REVIEW", "REJECTED"]
    risk_flags: list[str] = Field(default_factory=list)


def normalize_digits(value: str) -> str:
    """Convert only Devanagari digits; leave all other Unicode characters intact."""
    return value.translate(DEVANAGARI_DIGITS)


def _first_contextual_number(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_dynamically(raw_text: str) -> dict:
    """Extract a conservative fallback using nearby field labels when available."""
    normalized = normalize_digits(raw_text)
    account = _first_contextual_number(
        normalized,
        (
            r"(?:खाते\s*(?:क्र|क्रमांक)|खात[ेा]\s*(?:नंबर|नं)|account\s*(?:no\.?|number|#)?)\s*[:：#-]?\s*([0-9][0-9 -]{2,24})",
        ),
    )
    income_text = _first_contextual_number(
        normalized,
        (
            r"(?:वेतन|मासिक\s*(?:उत्पन्न|वेतन)|monthly\s*income|income|salary)\s*[:：₹Rs.\s-]*([0-9][0-9, ]*)",
        ),
    )

    if not account:
        candidates = re.findall(r"(?<!\d)\d{3,20}(?!\d)", normalized)
        candidates = [n for n in candidates if len(n) != 10 and not re.fullmatch(r"20\d{6}", n)]
        account = next((n for n in candidates if len(n) >= 9), candidates[0] if candidates else "")
    account = re.sub(r"\D", "", account)

    income = 0.0
    if income_text:
        digits = re.sub(r"\D", "", income_text)
        if digits:
            income = float(digits)
    else:
        for number in re.findall(r"(?<!\d)\d[\d, ]*(?!\d)", normalized):
            digits = re.sub(r"\D", "", number)
            if digits and len(digits) <= 7 and 5000 < int(digits) <= 100000000:
                income = float(digits)
                break

    pan_match = PAN_PATTERN.search(normalized.upper())
    name_match = re.search(r"(?:नाव|खातेदार(?:ाचे)?\s*नाव)\s*[:：]?\s*([^\n\r,]+)", raw_text)
    district_match = re.search(r"(?:जिल्हा|पत्ता)\s*[:：]?\s*([^\n\r,]+)", raw_text)
    first_line = raw_text.strip().splitlines()[0].strip() if raw_text.strip() else ""
    name_mr = name_match.group(1).strip() if name_match else "अर्जदार"

    return {
        "bank_or_society_name": first_line or "सहकारी संस्था",
        "applicant_name_marathi": name_mr,
        "applicant_name_english": name_mr,
        "account_number": account or "000000",
        "pan_number": pan_match.group(0) if pan_match else None,
        "monthly_income": income,
        "district": district_match.group(1).strip() if district_match else "Maharashtra",
    }


def assess(data: dict) -> dict:
    income = float(data.get("monthly_income") or 0.0)
    score = 50
    flags: list[str] = []

    account_digits = re.sub(r"\D", "", normalize_digits(str(data.get("account_number") or "")))
    has_pan = bool(data.get("pan_number"))

    # Fail-safe check: document has no account, no PAN, and no salary recorded
    if (not account_digits or account_digits == "000000") and not has_pan and income == 0.0:
        flags.extend(["UNREADABLE_DOCUMENT", "INCOMPLETE_ACCOUNT_NO", "MISSING_PAN", "LOW_INCOME_RISK"])
        data.update(
            eligibility_score=0,
            status="REJECTED",
            risk_flags=flags,
        )
        return data

    if income > 25000:
        score += 25
    elif income < 15000:
        score -= 15
        flags.append("LOW_INCOME_RISK")

    if has_pan:
        score += 15
    else:
        flags.append("MISSING_PAN")

    if len(account_digits) < 9:
        score -= 30
        flags.append("INCOMPLETE_ACCOUNT_NO")

    score = max(0, min(100, score))
    data.update(
        eligibility_score=score,
        status="AUTO_APPROVED" if score >= 75 else "MANUAL_REVIEW" if score >= 50 else "REJECTED",
        risk_flags=flags,
    )
    return data


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "cooplending-ai"}


@app.post("/api/v1/extract", response_model=LoanAssessment)
def extract(request: ExtractionRequest) -> LoanAssessment:
    clean_text = normalize_digits(request.raw_text)
    parsed: object = None

    if client is not None:
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": clean_text},
                ],
            )
            content = response.choices[0].message.content
            if content:
                parsed = json.loads(content)
                logger.info("LLM extraction completed")
        except Exception:
            logger.exception("LLM extraction failed; using local fallback")

    fallback = parse_dynamically(request.raw_text)
    if not isinstance(parsed, dict):
        parsed = fallback
    else:
        for key, value in fallback.items():
            # Never overwrite an explicit None PAN from the LLM
            if key == "pan_number":
                if key not in parsed:
                    parsed[key] = value
            elif key not in parsed or parsed[key] is None or parsed[key] == "":
                parsed[key] = value

    assessed = assess(parsed)
    return LoanAssessment(**assessed)