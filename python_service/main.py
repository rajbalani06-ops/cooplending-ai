import os
import re
import json
import logging
import unicodedata
from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("extraction_worker")

app = FastAPI(title="CoopLending AI Extraction Worker", version="1.0.0")

GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

if GROQ_KEY.startswith("gsk_"):
    client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=GROQ_KEY,
    )
    # Using the verified available active model from your account
    MODEL_NAME = "openai/gpt-oss-20b"
elif OPENAI_KEY and not OPENAI_KEY.startswith("dummy"):
    client = OpenAI(api_key=OPENAI_KEY)
    MODEL_NAME = "gpt-4o-mini"
else:
    client = None
    MODEL_NAME = "none"

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


def normalize_digits(value: str) -> str:
    return "".join(str(unicodedata.digit(ch)) if ch.isdigit() else ch for ch in value)


def parse_dynamically(raw_text: str) -> dict:
    """Dynamic parser that extracts real values from text if the LLM is unreachable."""
    normalized = normalize_digits(raw_text)

    # Extract account number and income
    nums = re.findall(r"\d+", normalized.replace(",", ""))
    income = 0.0
    account = ""
    for n in nums:
        if len(n) >= 9:
            account = n
        elif int(n) > 5000 and income == 0.0:
            income = float(n)
        elif not account and len(n) >= 3:
            account = n

    # Extract PAN if present
    pan_match = re.search(r"[A-Z]{5}[0-9]{4}[A-Z]", normalized.upper())
    pan = pan_match.group(0) if pan_match else None

    # Extract Name
    name_mr = "अर्जदार"
    name_match = re.search(r"(?:नाव|खातेदार|खातेदाराचे नाव)[:\s]+([^\n\r,]+)", raw_text)
    if name_match:
        name_mr = name_match.group(1).strip()

    # Extract Bank/Society
    first_line = raw_text.strip().split("\n")[0].strip()

    # Extract District
    dist_match = re.search(r"(?:जिल्हा|पत्ता)[:\s]+([^\n\r,]+)", raw_text)
    district = dist_match.group(1).strip() if dist_match else "Maharashtra"

    return {
        "bank_or_society_name": first_line if first_line else "सहकारी संस्था",
        "applicant_name_marathi": name_mr,
        "applicant_name_english": name_mr,
        "account_number": account if account else "000000",
        "pan_number": pan,
        "monthly_income": income if income else 15000.0,
        "district": district,
    }


def assess(data: dict) -> dict:
    score = 50
    flags = []
    income = float(data.get("monthly_income") or 0.0)

    if income > 25000:
        score += 25
    elif income < 15000:
        score -= 15
        flags.append("LOW_INCOME_RISK")

    if data.get("pan_number"):
        score += 15
    else:
        flags.append("MISSING_PAN")

    account_digits = re.sub(r"\D", "", normalize_digits(str(data.get("account_number") or "")))
    if len(account_digits) < 9:
        score -= 30
        flags.append("INCOMPLETE_ACCOUNT_NO")

    score = max(0, min(100, score))
    status = "AUTO_APPROVED" if score >= 75 else "MANUAL_REVIEW" if score >= 50 else "REJECTED"

    data["eligibility_score"] = score
    data["status"] = status
    data["risk_flags"] = flags
    return data


class ExtractionRequest(BaseModel):
    raw_text: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "cooplending-ai"}


@app.post("/api/v1/extract")
def extract(request: ExtractionRequest) -> dict:
    parsed = None
    clean_text = normalize_digits(request.raw_text)

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
                logger.info(f"LLM Extraction completed: {parsed.get('applicant_name_english')}")
        except Exception as e:
            logger.error(f"LLM API call failed: {e}")
            parsed = parse_dynamically(request.raw_text)
    else:
        parsed = parse_dynamically(request.raw_text)

    if not isinstance(parsed, dict):
        parsed = parse_dynamically(request.raw_text)

    # Fill in any missing default fields without overriding null PANs
    fallback = parse_dynamically(request.raw_text)
    for key, val in fallback.items():
        if key not in parsed:
            parsed[key] = val
        elif parsed[key] is None and key != "pan_number":
            parsed[key] = val

    return assess(parsed)