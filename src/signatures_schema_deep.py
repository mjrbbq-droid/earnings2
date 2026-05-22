# src/signatures_schema_deep.py
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Trend = Literal["improving", "stable", "deteriorating", "unclear"]
Level = Literal["low", "medium", "high", "unclear"]
Bias = Literal["raise", "reaffirm", "cut", "no_guidance", "unclear"]
Confidence = Literal["high", "medium", "low"]

class Quote(BaseModel):
    quote: str = Field(..., description="Short supporting quote (<=25 words).")
    speaker: Optional[str] = None
    section: Optional[Literal["prepared", "qa"]] = None

class HomebuildersDeepSignature(BaseModel):
    ticker: str
    call_date: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None

    # Demand / sales engine
    demand_trend: Trend
    traffic_leads: Trend
    conversion: Trend
    cancellations: Trend
    backlog_direction: Trend
    backlog_quality: Literal["strong", "normal", "weak", "unclear"]

    # Pricing / incentives / buydowns
    pricing_power: Trend
    incentive_intensity: Trend
    buydown_intensity: Trend
    discounting_mode: Literal["price_cuts", "incentives", "both", "unclear"]
    competitive_pressure: Level
    inventory_pressure: Level

    # Margin mechanics
    gross_margin_direction: Trend
    margin_driver_primary: Literal["incentives", "pricing", "costs", "mix", "volume_leverage", "unclear"]
    spec_mix_level: Level
    spec_mix_direction: Trend

    # Costs / operations
    build_cycle_times: Trend
    labor_costs: Trend
    materials_costs: Trend
    cost_actions: Level  # how active are rebids, SKU rationalization, value engineering

    # Land / communities
    community_count_direction: Trend
    land_spend_direction: Trend
    option_percentage_direction: Trend
    land_risk: Level  # impairments, renegotiations, walk-aways, underwriting tightening

    # Macro / affordability framing
    affordability: Trend
    mortgage_rate_sensitivity: Level

    # Outlook posture
    guidance_bias: Bias
    visibility_confidence: Confidence

    # What is helping vs hurting now
    tailwinds: List[str] = Field(default_factory=list)
    headwinds: List[str] = Field(default_factory=list)

    # What changed vs last call
    key_changes: List[str] = Field(default_factory=list)

    # Evidence
    supporting_quotes: List[Quote] = Field(default_factory=list)
