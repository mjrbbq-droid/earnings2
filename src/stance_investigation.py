# src/stance_investigation.py
"""
BALANCED per-ticker police-stance investigation.

For each company, captures BOTH:
  - ANTI-police actions (defund advocacy, refusing to sell to police,
    donations to reform NGOs, walked-back partnerships)
  - PRO-police actions (selling weapons/FR/body cams/radios to police,
    hosting police data, donations to police foundations/unions,
    marketing partnerships with law enforcement)

Then synthesizes a net_position. Uses Anthropic's web_search server-side
tool; one API call per ticker.
"""
from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

INVESTIGATION_VERSION = "stance-investigation-v3-refined-donations"

# ─── Anti-police taxonomy (v3 — donation buckets split) ─────────────────
# IMPORTANT: anti_police_action=true should be set ONLY when the dominant
# action is genuinely adversarial to police. Collaborative-reform, re-entry,
# and innocence-project funding do NOT qualify as anti-police even though
# they are criminal-justice-related.
AntiPoliceType = Literal[
    # ---- Genuinely adversarial-to-police (anti_police_action=true) -----
    "defund_police_explicit",
    "exit_facial_recognition_police",
    "refuse_facial_recognition_police",
    "moratorium_facial_recognition_police",
    "severed_police_partnership",
    "donations_anti_police_adversarial",      # funds groups that sue police, advocate defunding/abolition
    "boycott_police_products",
    # ---- Criminal-justice-adjacent but NOT anti-police -----------------
    "donations_collaborative_reform",         # funds reform that works WITH police (Policing Project @ NYU)
    "donations_reentry_employment",           # post-incarceration employment (Anti-Recidivism Coalition)
    "donations_innocence_wrongful_conviction", # Innocence Project family
    "donations_broad_criminal_justice_reform", # mixed CJ reform (Vera prosecutor work, etc.)
    # ---- Weak / none ---------------------------------------------------
    "statement_only",                         # generic BLM statement, no concrete action
    "none",
    "unknown",
]

# Set of types that should cause anti_police_action=True. Everything else
# is criminal-justice-adjacent but not adversarial to police.
ANTI_POLICE_ADVERSARIAL_TYPES: frozenset[str] = frozenset({
    "defund_police_explicit",
    "exit_facial_recognition_police",
    "refuse_facial_recognition_police",
    "moratorium_facial_recognition_police",
    "severed_police_partnership",
    "donations_anti_police_adversarial",
    "boycott_police_products",
})

# ─── Pro-police taxonomy ─────────────────────────────────────────────────
ProPoliceType = Literal[
    "manufactures_police_weapons",           # TASERs, restraints (AXON)
    "sells_facial_recognition_to_police",
    "sells_surveillance_tech_to_police",     # Palantir, predictive policing
    "sells_body_cameras_to_police",          # AXON, Motorola
    "sells_radios_comms_to_police",          # MSI, L3Harris
    "sells_data_analytics_to_police",        # PLTR
    "hosts_police_data_infrastructure",      # AWS / Azure police contracts
    "marketing_partnership_first_responders", # VZ Frontline, T FirstNet
    "donates_to_police_foundations",
    "donates_to_police_unions",
    "lobbies_for_police_funding",
    "public_statement_supporting_police",
    "none",
    "unknown",
]

CurrentStatus = Literal[
    "reinforced", "active", "maintained", "eroded",
    "suppressed", "faded", "walked_back", "expanded", "unknown",
]

NetPosition = Literal[
    "reform_leaning",        # clear reform-side signal, little/no enforcement exposure
    "enforcement_leaning",   # clear enforcement-side exposure, little/no reform
    "cross_exposure",        # both reform and enforcement actions present
    "no_material_exposure",  # neither — most S&P 500 companies
    "unknown",
]


class StanceInvestigation(BaseModel):
    # ── ANTI-police findings ─────────────────────────────────────────────
    anti_police_action: bool = Field(
        description="True if the company has taken any concrete public ANTI-police "
                    "action since 2018. Concrete = corporate policy change, donation, "
                    "boycott, severed contract, public advocacy. Not employee actions, "
                    "not generic BLM statements without a police angle."
    )
    anti_police_type: AntiPoliceType
    anti_police_first_date: str | None = Field(
        description="ISO YYYY-MM-DD (or YYYY-MM-01 if only month, YYYY-01-01 if only "
                    "year). Null if anti_police_action=false."
    )
    anti_police_first_year: int | None
    anti_police_last_known_date: str | None
    anti_police_summary: str | None = Field(
        description="One sentence describing the anti-police action with date and "
                    "named people/agencies. Null if anti_police_action=false."
    )
    anti_police_current_status: CurrentStatus
    anti_police_evidence_url: str | None
    anti_police_evidence_quote: str | None = Field(
        description="1-3 sentence direct quote from a source. Null if no action."
    )

    # ── PRO-police findings ──────────────────────────────────────────────
    pro_police_action: bool = Field(
        description="True if the company sells products/services TO police, donates "
                    "TO police foundations or unions, partners commercially with law "
                    "enforcement, or otherwise has material pro-police corporate "
                    "exposure. Examples: AXON (police weapons), MSI (police radios), "
                    "PLTR (predictive policing analytics), AWS/Azure (hosting police "
                    "data), VZ Frontline marketing."
    )
    pro_police_type: ProPoliceType
    pro_police_first_date: str | None
    pro_police_first_year: int | None
    pro_police_last_known_date: str | None
    pro_police_summary: str | None = Field(
        description="One sentence describing the pro-police exposure with date if "
                    "known. Be specific: 'Sells body cameras and TASERs to ~17,000 "
                    "US police departments' is better than 'public safety vendor'."
    )
    pro_police_current_status: CurrentStatus
    pro_police_evidence_url: str | None
    pro_police_evidence_quote: str | None

    # ── Net synthesis ────────────────────────────────────────────────────
    net_position: NetPosition = Field(
        description="Net read across BOTH sides. 'mixed' is REAL — Big Tech often "
                    "bans FR for police while hosting all of their email/data on "
                    "AWS or Azure. 'neutral' is the default for companies with no "
                    "police exposure either way."
    )
    net_summary: str = Field(
        description="One sentence balanced summary: 'Anti-police: X. Pro-police: Y. "
                    "Net: Z.' E.g.: 'Anti-police: Rekognition moratorium since 2020 "
                    "(extended 2021). Pro-police: AWS hosts data for thousands of "
                    "police depts, FBI uses Rekognition since 2024. Net: mixed.'"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = Field(
        description="Anything material that doesn't fit structured fields: walk-backs, "
                    "internal conflicts, recent reversals, parent-subsidiary tensions."
    )


SYSTEM_PROMPT = """You are a financial-risk analyst investigating BOTH SIDES of a
publicly-traded company's relationship with US police / law enforcement.

═══════════════════════════════════════════════════════════════════════════
CRITICAL TAXONOMY (read carefully — most "donations to police reform" are
NOT actually anti-police):

ADVERSARIAL anti-police actions → set anti_police_action=TRUE:
  - defund_police_explicit: company uses the phrase "defund the police" as
    corporate position. Almost unique to Ben & Jerry's.
  - exit_facial_recognition_police: permanently left an FR market because of
    police use (IBM).
  - refuse_facial_recognition_police: refused to sell FR to police (Microsoft).
  - moratorium_facial_recognition_police: paused FR sales to police (Amazon).
  - severed_police_partnership: ended a specific contract / partnership.
  - donations_anti_police_adversarial: funds groups that SUE police, advocate
    DEFUNDING or ABOLITION, or are explicitly adversarial to policing.
    Examples: BLM Foundation (defund.org), ArchCity Defenders (sues PDs),
    Movement for Black Lives, Critical Resistance, Minnesota Freedom Fund
    (cash-bail abolition), Color of Change criminal-justice campaigns.
  - boycott_police_products: stopped selling products marketed to police.

CRIMINAL-JUSTICE-ADJACENT but NOT anti-police → set anti_police_action=FALSE.
Use the right type so the data is auditable:
  - donations_collaborative_reform: funds reform that works WITH police.
    Examples: Policing Project at NYU Law (works with NYPD, LAPD, etc.),
    Equal Justice USA Trauma to Trust (cop+community facilitated dialogue),
    Center for Policing Equity, Police Executive Research Forum, body-cam
    advocacy. Chubb's Rule of Law Fund fits HERE.
  - donations_reentry_employment: post-incarceration employment, second-
    chance hiring. Examples: Anti-Recidivism Coalition, Defy Ventures,
    Center for Employment Opportunities, JPMorgan's Chase PolicyCenter /
    Second Chance Agenda, Apple REJI's anti-recidivism arm.
  - donations_innocence_wrongful_conviction: Innocence Project family.
  - donations_broad_criminal_justice_reform: general CJ reform that doesn't
    clearly fit above. Sentencing reform, prosecutor reform, Vera Institute
    Motion for Justice. Set anti_police_action based on the dominant
    grantee work — if the SPECIFIC funded programs sue or oppose police,
    flag adversarial; if they're collaborative or sentencing-focused, FALSE.

Weak / none → anti_police_action=FALSE:
  - statement_only: 2020 "we condemn..." statement with no concrete action.
  - none, unknown.

THE TEST: ask "do the SPECIFIC grantees this money flows to fight against
police, OR work alongside police?" Funding Policing Project at NYU is
working alongside. Funding ArchCity Defenders is fighting against. The
former is NOT anti-police; the latter IS.

NOT anti-police at all:
  - General DEI / EEOC matters
  - Gun-reform activism (different frame)
  - Racial-justice statements that don't mention police
  - Employee or executive personal actions (we want CORPORATE positions)

═══════════════════════════════════════════════════════════════════════════
PRO-police corporate exposures (any time period — these tend to be ongoing):
  - Manufactures weapons/restraints sold to police (AXON Taser, etc.)
  - Sells facial recognition, surveillance, predictive analytics to police
    (historical big tech, Palantir, Clearview before its scandal, etc.)
  - Sells body cameras / radios / dispatch / comms to police
    (AXON, Motorola Solutions, L3Harris)
  - Hosts police data infrastructure (AWS hosting police agencies, Azure
    running PD email and case management)
  - Marketing partnerships with police / first responders (Verizon Frontline,
    AT&T FirstNet)
  - Donations to police foundations or police unions
  - Lobbying for police funding
  - Major contracts with DOJ / FBI / state police

═══════════════════════════════════════════════════════════════════════════
NET POSITION

The most interesting finding is often "mixed" — Big Tech companies frequently
have public anti-police stances (FR bans) while running massive pro-police
infrastructure (hosting data, federal contracts).

Examples to calibrate (v3 refined taxonomy):
  - UL/Ben & Jerry's: anti_police_net — explicit defund advocacy (anti)
  - AXON: pro_police_net — manufactures police weapons (pro)
  - PLTR: pro_police_net — predictive policing analytics (pro)
  - MSI: pro_police_net — police radios + ALPR (pro)
  - MSFT: mixed — Azure OpenAI FR ban (anti) + Azure Govt CJIS hosting (pro)
  - AMZN: mixed — Rekognition moratorium (anti) + AWS police hosting (pro)
  - IBM: mixed — exited FR (anti) + DOJ/FBI Watson contracts (pro)
  - NKE: mixed — funds ArchCity Defenders (anti, adversarial)
    + Nike SFB tactical boot line for LE (pro)

CRITICAL — these are NOT anti-police even though they fund "criminal justice":
  - CB (Chubb Rule of Law Fund): donations_collaborative_reform.
    Funds Policing Project @ NYU, Equal Justice USA Trauma to Trust, Vera,
    Innocence Project. All work WITH police. anti_police_action=FALSE.
  - JPM (Chase PolicyCenter / Second Chance Agenda):
    donations_reentry_employment. Helps people with criminal records get
    jobs. Post-incarceration, not against police. anti_police_action=FALSE.
  - AAPL (Racial Equity & Justice Initiative): mostly
    donations_reentry_employment + donations_broad_criminal_justice_reform.
    Anti-Recidivism Coalition, Vera, Defy Ventures. anti_police_action=FALSE
    unless you find evidence of adversarial-grantee funding.

═══════════════════════════════════════════════════════════════════════════
INSTRUCTIONS

1. Use web_search to research BOTH directions.
2. DATES matter — surface first_action_date as ISO YYYY-MM-DD when possible.
3. Current status matters as much as the original action.
4. Be honest about "mixed" — don't pick a side if the company genuinely has both.
5. Most companies will be "neutral" with both flags false. That's fine.
6. Ground findings in real source URLs and short direct quotes."""


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def investigate_company(
    client: anthropic.Anthropic,
    *,
    ticker: str,
    company_name: str,
    sector: str | None = None,
) -> StanceInvestigation:
    """One balanced investigation per ticker. Claude does its own web_search."""
    sector_hint = f" (sector: {sector})" if sector else ""
    user_msg = (
        f"Investigate **{company_name}** (ticker ${ticker}){sector_hint}.\n\n"
        f"Research BOTH sides of this company's relationship with US police / law "
        f"enforcement:\n"
        f"  - ANTI-police: corporate stances, donations to reform groups, products "
        f"refused to police, walked-back partnerships.\n"
        f"  - PRO-police: products / services sold to police, hosting their data, "
        f"donations to police foundations or unions, marketing partnerships, lobbying.\n\n"
        f"Use web_search. Surface dates. Then synthesize a net_position."
    )

    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=12000,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_msg}],
        tools=[
            {"type": "web_search_20260209", "name": "web_search"},
        ],
        output_format=StanceInvestigation,
    )
    return response.parsed_output
