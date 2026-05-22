# src/signatures_schema_energy_services_v1.py
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Trend = Literal["improving", "stable", "deteriorating", "unclear"]
Level = Literal["low", "medium", "high", "unclear"]

EarningsAction = Literal["raised", "cut", "reaffirmed", "withdrawn", "no_guidance", "unclear"]
TimingBias = Literal["front_half", "back_half", "even", "unclear"]
Visibility = Literal["high", "medium", "low", "unclear"]

MarginOutlook = Literal["expanding", "stable", "contracting", "unclear"]
MixShift = Literal["offshore_favoring", "onshore_favoring", "intl_favoring", "us_favoring", "mixed", "unclear"]
PricingMode = Literal["spot", "contract", "mixed", "unclear"]
WorkingCapital = Literal["source", "neutral", "use", "unclear"]

class Quote(BaseModel):
    quote: str = Field(..., description="Short supporting quote (<=25 words).")
    speaker: Optional[str] = None
    section: Optional[Literal["prepared", "qa"]] = None

class EnergyServicesDeepSignatureV1(BaseModel):
    # Metadata (from call header)
    ticker: str
    call_date: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None

    # Activity / utilization
    activity_trend: Trend = Field(..., description="Customer activity (rig/frac/offshore cadence).")
    utilization_level: Level = Field(..., description="Tight vs slack capacity utilization.")
    utilization_trend: Trend = Field(..., description="Utilization trending up/down.")

    # Orders / backlog / visibility
    backlog_trend: Trend
    bookings_trend: Trend
    book_to_bill: Literal["above_1", "around_1", "below_1", "unclear"]
    backlog_duration: Literal["short_cycle", "medium_cycle", "long_cycle", "unclear"]
    project_pushouts: Level = Field(..., description="Degree of project push-outs / timing slippage.")
    visibility_confidence: Visibility

    # Pricing / rates / pass-through
    pricing_power: Trend
    dayrate_trend: Trend
    pricing_mode: PricingMode
    pass_through_success: Trend = Field(..., description="Ability to pass costs/tariffs through to customers.")
    competitive_pressure: Level

    # Costs / inflation / tariffs
    cost_pressure: Trend
    tariff_or_supply_chain_impact: Level

    # Mix
    mix_shift: MixShift
    offshore_exposure_trend: Trend
    international_exposure_trend: Trend

    # Margins
    gross_margin_outlook: MarginOutlook
    operating_margin_outlook: MarginOutlook
    margin_driver_primary: Literal["utilization", "pricing", "mix", "costs", "working_capital", "other", "unclear"]

    # Customer budgets / capex tone
    customer_capex_tone: Trend = Field(..., description="Customer budget tone / willingness to spend.")
    customer_budget_visibility: Visibility

    # Working capital / cash conversion
    working_capital_direction: WorkingCapital
    cash_conversion: Trend
    balance_sheet_stress: Level
    leverage_trend: Trend
    liquidity_trend: Trend

    # Guidance posture
    earnings_guidance_action: EarningsAction
    earnings_timing_bias: TimingBias
    earnings_visibility: Visibility

    # What changed / what matters
    tailwinds: List[str] = Field(default_factory=list)
    headwinds: List[str] = Field(default_factory=list)
    key_changes: List[str] = Field(default_factory=list)

    supporting_quotes: List[Quote] = Field(default_factory=list)
