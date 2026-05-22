# src/signatures_schema_construction_engineering_v1.py
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Trend = Literal["improving", "stable", "deteriorating", "unclear"]
Level = Literal["low", "medium", "high", "unclear"]

EarningsAction = Literal["raised", "cut", "reaffirmed", "withdrawn", "no_guidance", "unclear"]
TimingBias = Literal["front_half", "back_half", "even", "unclear"]
Visibility = Literal["high", "medium", "low", "unclear"]

MarginOutlook = Literal["expanding", "stable", "contracting", "unclear"]
ProjectMix = Literal["public_favoring", "private_favoring", "balanced", "unclear"]
PricingMode = Literal["fixed_price", "cost_plus", "mixed", "unclear"]
WorkingCapital = Literal["source", "neutral", "use", "unclear"]


class Quote(BaseModel):
    quote: str = Field(..., description="Short supporting quote (<=25 words).")
    speaker: Optional[str] = None
    section: Optional[Literal["prepared", "qa"]] = None


class ConstructionEngineeringDeepSignatureV1(BaseModel):
    # Metadata
    ticker: str
    call_date: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None

    # Backlog / pipeline / awards
    backlog_trend: Trend = Field(..., description="Direction of total backlog.")
    backlog_quality: Trend = Field(..., description="Margin quality of backlog (mix of higher vs lower margin work).")
    new_awards_trend: Trend = Field(..., description="Pace of new project awards / bookings.")
    book_to_burn: Literal["above_1", "around_1", "below_1", "unclear"] = Field(
        ..., description="Book-to-burn ratio (new awards vs revenue recognized)."
    )
    bid_pipeline_trend: Trend = Field(..., description="Size / health of bid pipeline / pending proposals.")
    project_pushouts: Level = Field(..., description="Degree of project delays or timing slippage.")
    visibility_confidence: Visibility

    # Revenue / end-market mix
    revenue_trend: Trend
    project_mix: ProjectMix = Field(..., description="Public (govt/infra) vs private sector revenue tilt.")
    infrastructure_exposure_trend: Trend = Field(
        ..., description="Exposure to infrastructure / federal / state spending."
    )
    end_market_diversification: Trend = Field(
        ..., description="Broadening or narrowing of end-market exposure."
    )

    # Pricing / contract structure
    pricing_power: Trend
    contract_mix: PricingMode = Field(..., description="Fixed-price vs cost-plus contract mix.")
    competitive_pressure: Level
    pass_through_success: Trend = Field(..., description="Ability to pass cost inflation to customers.")

    # Costs / labor / materials
    cost_pressure: Trend
    labor_availability: Trend = Field(..., description="Skilled labor availability / tightness.")
    labor_cost_trend: Trend
    materials_cost_trend: Trend
    subcontractor_availability: Trend
    tariff_or_supply_chain_impact: Level

    # Margins
    gross_margin_outlook: MarginOutlook
    operating_margin_outlook: MarginOutlook
    margin_driver_primary: Literal[
        "project_mix", "pricing", "labor", "materials", "utilization",
        "execution", "overhead_leverage", "other", "unclear"
    ]

    # Project execution
    project_execution_quality: Trend = Field(
        ..., description="Track record on project delivery (on time/budget, change orders, claims)."
    )
    claims_or_disputes: Level = Field(..., description="Level of outstanding claims or legal disputes.")

    # Balance sheet / capital
    working_capital_direction: WorkingCapital
    cash_conversion: Trend
    balance_sheet_stress: Level
    leverage_trend: Trend
    liquidity_trend: Trend
    bonding_capacity: Trend = Field(..., description="Surety bonding capacity trend.")
    capital_return_posture: Literal[
        "increasing", "steady", "decreasing", "suspended", "unclear"
    ]

    # Guidance posture
    earnings_guidance_action: EarningsAction
    earnings_timing_bias: TimingBias
    earnings_visibility: Visibility

    # What changed / what matters
    tailwinds: List[str] = Field(default_factory=list)
    headwinds: List[str] = Field(default_factory=list)
    key_changes: List[str] = Field(default_factory=list)

    supporting_quotes: List[Quote] = Field(default_factory=list)
