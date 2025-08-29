"""
Minimal sample tools for Surfari's tool-call framework.

- Pass the callables directly to your normalizers:
    _normalize_tools_for_openai([report_account_details, report_investment_positions])
    _normalize_tools_for_gemini([report_account_details, report_investment_positions])

- Each tool:
  * Validates inputs via Pydantic models
  * Returns ONLY: {"ok": bool, "summary": str}
"""

from typing import Any, List, Optional, Dict, Union
from pydantic import BaseModel, Field
from decimal import Decimal
from surfari.model.tool_helper import _ensure_list_of_models

# -------- Models --------
class Account(BaseModel):
    account_name: str = Field(..., description="The name of the account")
    account_num: Optional[str] = Field(None, description="The number of the account")
    account_value: str = Field(..., description="The value/balance of the account")
    account_type: Optional[str] = Field(None, description="The type/category of the account")

class InvestmentPosition(BaseModel):
    symbol: str = Field(..., description="Ticker symbol, use CASH for cash positions")
    name: Optional[str] = Field(None, description="Instrument name")
    quantity: int = Field(..., description="Quantity/Shares")
    price: float = Field(..., description="Unit price (use 1 for CASH)")
    cost_basis: Optional[str] = Field(None, description="Cost basis as text")
    market_value: Optional[str] = Field(None, description="Market value as text")
    day_change_amount: Optional[str] = Field(None, description="Daily change as text")
    day_change_percentage: Optional[str] = Field(None, description="Daily change percentage as text")
    position_type: Optional[str] = Field(None, description="Type/category of position")
    percent_of_holdings: Optional[str] = Field(None, description="Percent of total holdings as text")

# -------- Tools (minimal returns) --------
def report_account_details(accounts: List[Account]) -> Dict[str, Any]:
    """
    Extract account details from scraped text.
    Returns only: {"ok": bool, "summary": str}
    """
    print("Calling function: report_account_details")
    print("Accounts:", accounts)
    valid, invalid = _ensure_list_of_models(accounts, Account)
    summary = f"accounts={len(valid)}; invalid={invalid}"
    return {"ok": True, "summary": summary}

def report_investment_positions(holdings: List[InvestmentPosition]) -> Dict[str, Any]:
    """
    Extract investment holding details from scraped text.
    Returns only: {"ok": bool, "summary": str}
    """
    print("Calling function: report_investment_positions")
    print("Holdings:", holdings)
    valid, invalid = _ensure_list_of_models(holdings, InvestmentPosition)
    summary = f"positions={len(valid)}; invalid={invalid}"
    return {"ok": True, "summary": summary}

tools = [report_account_details, report_investment_positions]
