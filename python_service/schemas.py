"""Compatibility exports; the canonical, self-contained schemas live in main.py."""

from main import ExtractionRequest, ExtractedLoanData, LoanAssessment

__all__ = ["ExtractionRequest", "ExtractedLoanData", "LoanAssessment"]
