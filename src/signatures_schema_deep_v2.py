# src/signatures_schema_deep_v2.py
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Trend = Literal["improving", "stable", "deteriorating", "unclear"]
Level = Literal["low", "medium", "high", "unclear"]

# Sales / earnings / balance sheet additions
SalesTrend = Literal["improving", "stable", "deteriorating", "unclear"]
PriceVsVolume = Literal["price_led", "volume_led", "mixed", "unclear"]

EarningsAction = Literal["raised", "cut", "reaffirmed", "withdrawn", "no_guidance", "unclear"]
TimingBias = Literal["front_half", "back_half", "even", "unclear"]
Visibility = Literal["high", "medium", "low", "unclear"]

MarginOutlook = Literal["expanding", "stable", "contracting", "unclear"]

class Quote(BaseModel):
    quote: str = Field(..., description="Short supporting quote (<=25 words).")
    speaker: Optional[str] = None
    section: Optional[Literal["prepared", "qa"]] = None


class HomebuildersDeepSignatureV2(BaseModel):
    # Metadata (from call header)
    ticker: str
    call_date: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None

    # --- CORE HOMEbuilder mechanics (carry-over) ---

    # Demand / funnel
    demand_trend: Trend
    traffic_leads: Trend
    conversion: Trend
    cancellations: Trend

    # Orders / backlog
    orders_trend: Trend
    backlog_direction: Trend
    backlog_quality: Literal["strong", "normal", "weak", "unclear"]

    # Pricing / incentives / buydowns
    pricing_power: Trend
    incentive_intensity: Trend
    buydown_intensity: Trend
    discounting_mode: Literal["price_cuts", "incentives", "both", "unclear"]
    competitive_pressure: Level
    inventory_pressure: Level

    # Spec / inventory posture
    spec_mix_level: Level
    spec_mix_direction: Trend

    # Margins (mechanics + outlook)
    gross_margin_direction: Trend
    gross_margin_outlook: MarginOutlook
    operating_margin_outlook: MarginOutlook
    margin_driver_primary: Literal["incentives", "pricing", "costs", "mix", "volume_leverage", "unclear"]

    # Costs / operations
    build_cycle_times: Trend
    labor_costs: Trend
    materials_costs: Trend
    cost_actions: Level  # how active are rebids/value engineering/SKU rationalization

    # Land / communities
    community_count_direction: Trend
    land_spend_direction: Trend
    option_percentage_direction: Trend
    land_risk: Level  # impairments/renegotiations/walk-aways/underwriting tightening

    # Macro / affordability framing
    affordability: Trend
    mortgage_rate_sensitivity: Level

    # Guidance posture
    earnings_guidance_action: EarningsAction
    earnings_timing_bias: TimingBias
    earnings_visibility: Visibility
    guidance_confidence: Visibility  # overall tone about visibility/confidence

    # --- NEW: Sales reality layer (what you asked for) ---
    sales_trend: SalesTrend
    volume_trend: SalesTrend
    price_vs_volume: PriceVsVolume

    # --- NEW: Balance sheet / liquidity layer ---
    balance_sheet_stress: Level
    leverage_trend: Trend
    liquidity_trend: Trend
    capital_return_posture: Literal["aggressive", "moderate", "conservative", "unclear"]  # buybacks/divs/deleveraging

    # What is helping vs hurting now
    tailwinds: List[str] = Field(default_factory=list)
    headwinds: List[str] = Field(default_factory=list)

    # What changed vs last call (explicit deltas)
    key_changes: List[str] = Field(default_factory=list)

    # Evidence
    supporting_quotes: List[Quote] = Field(default_factory=list)
