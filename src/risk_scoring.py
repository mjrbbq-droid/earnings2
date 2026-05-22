# src/risk_scoring.py
"""
Claude-powered article scoring for institutional_risk.db.

Score = (relevance, stance, severity, confidence, rationale)
- relevance:  0-100; is this article actually about the topic, or is the keyword hit coincidental?
- stance:     activism | positive_institutional | neutral | irrelevant
- severity:   -5 .. +5  (sign = direction; +activism / -institutional; magnitude = signal strength)
- confidence: 0.0-1.0
- rationale:  1-2 sentence justification

Model: Claude Opus 4.7, adaptive thinking, prompt-cached system instruction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

PROMPT_VERSION = "risk-score-v1"                                # legacy alias = v1 snippet
PROMPT_VERSION_V1_SNIPPET            = "risk-score-v1"
PROMPT_VERSION_V2_FULLTEXT           = "risk-score-v2-fulltext"
PROMPT_VERSION_V3_PY_CLAUDE_RATIONALE = "risk-score-v3-py-rationale"

# Tier-1 domains get a relevance + 1-magnitude boost (best-effort, not exhaustive)
TIER1_DOMAINS: frozenset[str] = frozenset({
    "wsj.com", "nytimes.com", "ft.com", "reuters.com", "bloomberg.com",
    "cnbc.com", "washingtonpost.com", "theguardian.com", "ap.org",
    "npr.org", "businessinsider.com", "axios.com", "politico.com",
})


def relative_date_label(age_days: int | None) -> str:
    """Human-readable 'yesterday' / '3 weeks ago' / '5 years ago' label."""
    if age_days is None:
        return "unknown date"
    if age_days < 0:
        return f"in {-age_days}d (future)"
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "yesterday"
    if age_days < 7:
        return f"{age_days} days ago"
    if age_days < 30:
        return f"{age_days // 7} week{'s' if age_days // 7 > 1 else ''} ago"
    if age_days < 365:
        m = age_days // 30
        return f"{m} month{'s' if m > 1 else ''} ago"
    y = age_days // 365
    return f"{y} year{'s' if y > 1 else ''} ago"


def recency_factor(published_iso: str | None, as_of: datetime | None = None) -> tuple[float, int | None]:
    """
    Return (multiplier, age_days). Older infractions weigh less.
        0-7    days  -> 1.00
        8-30   days  -> 0.90
        31-90  days  -> 0.70
        91-365 days  -> 0.50
        >1 year      -> 0.30
        unknown date -> 0.80 (slight penalty, can't verify recency)
    """
    if not published_iso:
        return 0.80, None
    as_of = as_of or datetime.now(timezone.utc)
    try:
        # Handle YYYY-MM-DD HH:MM:SS or ISO
        pub = datetime.fromisoformat(published_iso.replace("Z", "+00:00").replace(" ", "T"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.80, None

    age_days = (as_of - pub).days
    if age_days < 0:
        return 1.00, age_days   # future-dated (data oddity) — treat as fresh
    if age_days <= 7:    return 1.00, age_days
    if age_days <= 30:   return 0.90, age_days
    if age_days <= 90:   return 0.70, age_days
    if age_days <= 365:  return 0.50, age_days
    return 0.30, age_days

SYSTEM_PROMPT = """You are a senior financial-risk analyst specializing in reputational and
institutional risk for public companies.

Given a news article (headline + snippet + ticker context), score it across these dimensions.

────────────────────────────────────────────────────────────────────────────
RELEVANCE  (integer, 0-100)
  Is the article actually about institutional risk in one of these themes:
  police / law enforcement / activism / governance / public safety / civil
  rights / boycotts / DEI / discrimination / regulatory action / protest?

  Score 0-20  : keyword match is coincidental (e.g., "BLM" matched a ticker,
                "police" used in a non-institutional sense, generic press release).
  Score 21-50 : article touches the theme tangentially.
  Score 51-80 : article is substantively about the theme.
  Score 81-100: article is centrally about the theme and directly material.

STANCE  (one of):
  activism                = pressure ON institutions — protests, boycotts,
                             defund campaigns, anti-corporate activism, reform
                             demands from outside, employee activism.
  positive_institutional   = pro-establishment framing — companies funding
                             police, "back the blue", public-safety tech sales,
                             law-enforcement partnerships.
  neutral                  = the keyword appears descriptively without a clear
                             institutional-risk angle ("police pulled a truck
                             from a lake").
  irrelevant               = relevance is low; the keyword hit is coincidental.

SEVERITY  (integer, -5 .. +5, SIGNED)
  Sign     : + = activism direction, − = positive_institutional direction, 0 = neutral/irrelevant.
  Magnitude (use absolute value):
    0 : no institutional-risk impact
    1 : minor headline noise, single thin source
    2 : single substantive story, one source
    3 : tier-1 source OR multi-source story on a real event
    4 : organized event — regulatory action, named-executive controversy,
        coordinated boycott, multi-day cluster of coverage
    5 : systemic crisis — earnings impact, enforcement action, lawsuit,
        CEO resignation, broad consumer backlash

CONFIDENCE  (float, 0.0-1.0)
  How confident are you in this score given ONLY the headline + snippet?
  Lower confidence if: paywalled source, ambiguous framing, snippet too short,
  ticker context unclear.

RATIONALE  (1-2 sentences)
  Explain the score. Be specific about WHAT in the article drove the call.
────────────────────────────────────────────────────────────────────────────

Be calibrated. Most articles are noise. A "DEI" keyword hit on a generic
press release is severity 0-1, not 3. A real regulatory investigation
(EEOC, FCC license review) is 3-4. An earnings-affecting boycott is 5."""


SYSTEM_PROMPT_V2_FULLTEXT = """You are a senior financial-risk analyst specializing in reputational and
institutional risk for public companies.

Given a news article (headline + full body + ticker context), score it across these dimensions.

You have the FULL article body, not just a snippet. Use it: ground your
rationale in specific phrasing from the article. If the article quotes a
regulator, names an executive, or describes a concrete action, that detail
should drive the severity and stance call.

────────────────────────────────────────────────────────────────────────────
RELEVANCE  (integer, 0-100)
  Is the article actually about institutional risk in one of these themes:
  police / law enforcement / activism / governance / public safety / civil
  rights / boycotts / DEI / discrimination / regulatory action / protest?

  Score 0-20  : keyword match is coincidental (e.g., "BLM" matched a ticker,
                "police" used in a non-institutional sense, generic press release).
  Score 21-50 : article touches the theme tangentially.
  Score 51-80 : article is substantively about the theme.
  Score 81-100: article is centrally about the theme and directly material.

STANCE  (one of):
  activism                = pressure ON institutions — protests, boycotts,
                             defund campaigns, anti-corporate activism, reform
                             demands from outside, employee activism.
  positive_institutional   = pro-establishment framing — companies funding
                             police, "back the blue", public-safety tech sales,
                             law-enforcement partnerships, government rollback
                             of activist-aligned policy (anti-DEI enforcement).
  neutral                  = the keyword appears descriptively without a clear
                             institutional-risk angle.
  irrelevant               = relevance is low; the keyword hit is coincidental.

  Stance hint: distinguish "activism PRESSURING a company" from "the government
  PRESSURING the company to roll back activist-aligned policy". The first is
  +activism. The second is -positive_institutional (anti-activism enforcement).
  Read carefully WHO is doing the pressuring and from which direction.

SEVERITY  (integer, -5 .. +5, SIGNED)
  Sign     : + = activism direction, − = positive_institutional direction, 0 = neutral/irrelevant.
  Magnitude (use absolute value):
    0 : no institutional-risk impact
    1 : minor passing mention
    2 : single substantive story
    3 : tier-1 source on a real event with named parties
    4 : organized event — regulatory action, named-executive controversy,
        coordinated boycott, multi-day coverage cluster, lawsuit, concrete
        enforcement action
    5 : systemic crisis — earnings impact, formal enforcement, lawsuit
        materially threatening the business, CEO resignation tied to the issue,
        broad consumer backlash with measurable financial impact

CONFIDENCE  (float, 0.0-1.0)
  How confident are you in this score given the full article body?
  Higher confidence when: named regulators / executives / quoted statements,
  tier-1 source, concrete actions described.
  Lower confidence when: framing is genuinely ambiguous, body contradicts
  itself, single anonymous source, opinion piece without reporting.

RATIONALE  (1-2 sentences)
  Explain the score, citing SPECIFIC content from the article body — names,
  quotes, actions. "The article quotes FCC chair Carr saying..." is better
  than "The article is about FCC action".
────────────────────────────────────────────────────────────────────────────

Be calibrated. Most articles are noise. A "DEI" keyword hit on a generic
press release is severity 0-1, not 3. A real regulatory investigation
(EEOC, FCC license review) is 3-4. An earnings-affecting boycott is 5."""


class ArticleScore(BaseModel):
    relevance: int = Field(ge=0, le=100)
    stance: Literal["activism", "positive_institutional", "neutral", "irrelevant"]
    severity: int = Field(ge=-5, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


def _build_user_content(article: dict) -> str:
    return (
        f"TICKER:           {article.get('ticker') or '(none)'}\n"
        f"PUBLISHED:        {article.get('published') or '(unknown)'}\n"
        f"SOURCE:           {article.get('source') or '(unknown)'}\n"
        f"KEYWORDS_MATCHED: {article.get('keywords') or '(none)'}\n"
        f"HEADLINE:         {article.get('title') or ''}\n"
        f"SNIPPET:          {article.get('snippet') or ''}\n"
    )


def _build_user_content_fulltext(article: dict) -> str:
    return (
        f"TICKER:           {article.get('ticker') or '(none)'}\n"
        f"PUBLISHED:        {article.get('published') or '(unknown)'}\n"
        f"SOURCE:           {article.get('source') or '(unknown)'}\n"
        f"DOMAIN:           {article.get('domain') or '(unknown)'}\n"
        f"FETCH_SOURCE:     {article.get('fetch_source') or 'origin'}\n"
        f"KEYWORDS_MATCHED: {article.get('keywords') or '(none)'}\n"
        f"HEADLINE:         {article.get('title') or ''}\n\n"
        f"ARTICLE BODY (extracted, {len(article.get('body') or '')} chars):\n"
        f"---\n"
        f"{article.get('body') or ''}\n"
        f"---\n"
    )


def score_article(client: anthropic.Anthropic, article: dict) -> ArticleScore:
    """v1 scoring: headline + snippet only."""
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": _build_user_content(article)}],
        output_format=ArticleScore,
    )
    return response.parsed_output


def score_article_fulltext(client: anthropic.Anthropic, article: dict) -> ArticleScore:
    """v2 scoring: headline + full article body."""
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT_V2_FULLTEXT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": _build_user_content_fulltext(article)}],
        output_format=ArticleScore,
    )
    return response.parsed_output


# ─── v3 hybrid: Python computes the numeric score, Claude writes the rationale ──
def score_python(article: dict, hits: list[dict], as_of: datetime | None = None) -> dict:
    """
    Deterministic numeric score from keyword hits + article metadata + recency.

    `hits` is a list of dicts with keys: severity (int 1-5), stance
    (activism | positive_institutional | neutral | None), keyword (str).

    Rules:
      - Sum severities within each stance bucket -> the dominant direction wins.
      - sign(severity) = +1 activism, -1 positive_institutional, 0 otherwise.
      - magnitude_raw  = dominant_bucket_severity_sum + tier1_boost.
      - magnitude_decayed = round(magnitude_raw × recency_factor(article.published)).
      - relevance is also dampened by recency.
      - confidence: high when hits agree, low when activism and institutional both fire.

    Returns dict with: relevance, stance, severity (decayed, signed),
    severity_raw (pre-decay, signed), confidence, age_days, recency_factor.
    """
    rf, age_days = recency_factor(article.get("published"), as_of=as_of)
    recency_source = "publish" if age_days is not None else "none"

    if not hits:
        return {
            "relevance": 0, "stance": "irrelevant", "severity": 0, "severity_raw": 0,
            "confidence": 0.95, "age_days": age_days, "recency_factor": rf,
            "recency_source": recency_source,
        }

    activism_w = sum(h["severity"] for h in hits if h.get("stance") == "activism")
    inst_w     = sum(h["severity"] for h in hits if h.get("stance") == "positive_institutional")
    neutral_w  = sum(h["severity"] for h in hits if (h.get("stance") or "neutral") == "neutral")

    domain = (article.get("domain") or "").lower()
    is_tier1 = domain in TIER1_DOMAINS
    ticker_watched = bool(article.get("ticker_in_watchlist"))

    # ── Direction ───────────────────────────────────────────────────────
    if activism_w > inst_w and activism_w > 0:
        stance, sign, magnitude = "activism", 1, activism_w
    elif inst_w > activism_w and inst_w > 0:
        stance, sign, magnitude = "positive_institutional", -1, inst_w
    elif activism_w > 0 and activism_w == inst_w:
        stance, sign, magnitude = "neutral", 0, 0  # mixed direction
    else:
        stance, sign = ("neutral", 0) if neutral_w > 0 else ("irrelevant", 0)
        magnitude = neutral_w if stance == "neutral" else 0

    if is_tier1 and sign != 0:
        magnitude += 1
    magnitude_raw = max(0, min(5, magnitude))
    severity_raw  = sign * magnitude_raw

    # Apply recency decay to the magnitude (direction never decays)
    magnitude_decayed = max(0, min(5, round(magnitude_raw * rf)))
    severity = sign * magnitude_decayed

    # ── Relevance ───────────────────────────────────────────────────────
    total_hit_weight = sum(h["severity"] for h in hits)
    relevance = total_hit_weight * 15
    if is_tier1:
        relevance += 10
    if ticker_watched:
        relevance += 10
    # Recency dampens relevance, but less harshly than severity (active investigations
    # written about months ago are still relevant)
    relevance = relevance * (0.5 + 0.5 * rf)
    relevance = max(0, min(100, int(round(relevance))))

    if stance == "irrelevant":
        relevance = min(relevance, 20)

    # ── Confidence ──────────────────────────────────────────────────────
    has_mixed = activism_w > 0 and inst_w > 0
    if has_mixed:
        confidence = 0.50
    elif len(hits) >= 3:
        confidence = 0.85
    elif len(hits) == 2:
        confidence = 0.75
    elif total_hit_weight >= 4:
        confidence = 0.70
    else:
        confidence = 0.55

    # Old articles get a small confidence penalty (could be stale info)
    if age_days is not None and age_days > 365:
        confidence = max(0.40, confidence - 0.10)

    return {
        "relevance": relevance,
        "stance": stance,
        "severity": severity,
        "severity_raw": severity_raw,
        "confidence": round(confidence, 2),
        "age_days": age_days,
        "recency_factor": rf,
        "recency_source": recency_source,
    }


def refine_score_with_event_date(
    score: dict,
    event_date: str | None,
    *,
    as_of: datetime | None = None,
) -> dict:
    """
    Re-decay severity using the extracted event_date when available.

    Use case: an article published yesterday about a 2015 event should weigh
    less than one about an event last week. Python's first pass decays on
    publish date; this pass replaces that with event-date decay once Claude
    has extracted the event_date.

    Stance, sign, severity_raw, confidence are unchanged. Only the
    recency-dependent fields (severity magnitude, age_days, recency_factor,
    recency_source) get re-derived. Relevance also re-dampened.
    """
    if not event_date:
        # No event date extracted — keep publish-based decay
        return score

    rf, age_days = recency_factor(event_date, as_of=as_of)
    sign_raw = 1 if score["severity_raw"] > 0 else -1 if score["severity_raw"] < 0 else 0
    magnitude_raw = abs(score["severity_raw"])
    magnitude_decayed = max(0, min(5, round(magnitude_raw * rf)))
    severity = sign_raw * magnitude_decayed

    # Relevance: re-apply dampening (but only the recency component)
    # Strip prior recency dampening: old_rel_was = relevance_pre / (0.5 + 0.5 * old_rf)
    # We don't have relevance_pre stored, so apply a conservative event-date adjustment:
    # take current relevance and rescale by event_rf / publish_rf.
    old_rf = score.get("recency_factor") or 1.0
    rel_scale = (0.5 + 0.5 * rf) / max(0.5 + 0.5 * old_rf, 1e-6)
    relevance = max(0, min(100, int(round(score["relevance"] * rel_scale))))

    # Confidence: penalty if event is very old
    confidence = score["confidence"]
    if age_days is not None and age_days > 365:
        confidence = max(0.40, confidence - 0.10)

    return {
        **score,
        "severity": severity,
        "age_days": age_days,
        "recency_factor": rf,
        "recency_source": "event",
        "relevance": relevance,
        "confidence": round(confidence, 2),
    }


class RationaleAndExtraction(BaseModel):
    """
    Claude's output when the numeric score is precomputed by Python.

    Claude does NOT re-score; it extracts structured facts from the body and
    writes the rationale. It may flag disagreement with the precomputed score
    via the `disagrees_with_score` flag and `disagreement_note`.
    """
    event_date: str | None = Field(
        description="ISO date YYYY-MM-DD of the underlying event/infraction "
                    "the article reports on, NOT the article's publish date. "
                    "Null if the body doesn't specify a clear event date."
    )
    infraction_type: Literal[
        "regulatory_investigation",   # ongoing probe, no formal action yet
        "regulatory_enforcement",     # formal action, license review, fine, ruling
        "lawsuit",
        "settlement",
        "boycott",
        "protest",
        "employee_activism",
        "executive_controversy",
        "policy_rollback",            # gov pressuring company to roll back activist policy
        "advocacy_campaign",          # NGO/advocacy pressure
        "none",                       # no infraction described
    ]
    infraction_summary: str = Field(
        description="One sentence: WHO did WHAT to WHOM. Use names, agencies, "
                    "specific actions. E.g.: 'FCC chair Brendan Carr ordered "
                    "an early renewal review of Disney's 8 ABC station licenses, "
                    "citing alleged DEI discrimination violations.'"
    )
    context_note: str = Field(
        description="One sentence on broader context: is this part of a pattern, "
                    "first occurrence, escalation/de-escalation, related to a "
                    "named broader initiative (e.g., 'part of Trump-administration "
                    "anti-DEI federal pressure campaign')?"
    )
    rationale: str = Field(
        description="One sentence justifying the precomputed Python score, "
                    "citing specific body content."
    )
    disagrees_with_score: bool = Field(
        description="True if you think the precomputed Python score is wrong "
                    "after reading the body."
    )
    disagreement_note: str | None = Field(
        description="If disagrees_with_score is True, one sentence on what's "
                    "wrong and what it should be. Else null."
    )


RATIONALE_SYSTEM_PROMPT = """You are a senior financial-risk analyst.

A deterministic Python rule-engine has already computed a numeric score from
the article's keyword hits and metadata:
    relevance / stance / severity / confidence
You will see that precomputed score in the user message.

Your job is NOT to re-score. Your job is to extract structured context and
write a rationale.

For each article, return:

  event_date           — ISO date YYYY-MM-DD of when the underlying event
                          happened (NOT when the article was published).
                          E.g.: an article published 2026-05-06 about a lawsuit
                          filed 2024-11-20 -> event_date = 2024-11-20.
                          If the body doesn't pin down a clear date, return null.
                          (We care because old infractions matter less than fresh ones.)

  infraction_type      — exactly one of the enumerated types.

  infraction_summary   — one sentence: WHO did WHAT to WHOM. Be specific:
                          name regulators, executives, agencies, dollar amounts,
                          named actions. NOT generic ("DEI controversy").

  context_note         — one sentence on broader context: is this part of a
                          named pattern (anti-DEI federal pressure campaign,
                          post-Floyd policing debate, X movement) or a one-off?
                          Is it escalating or de-escalating?

  rationale            — one sentence justifying the precomputed score using
                          specifics from the body.

  disagrees_with_score — true if, after reading the body, you think the score
                          is materially wrong (e.g. wrong stance direction,
                          severity off by 2+). Otherwise false.

  disagreement_note    — one sentence on what the score gets wrong if you
                          flagged disagreement. Else null.

Be concise. Cite the body, don't paraphrase generically."""


def write_rationale(client: anthropic.Anthropic, article: dict, score: dict) -> RationaleAndExtraction:
    """
    Claude extracts event_date / infraction_type / infraction_summary /
    context_note / rationale / disagreement_flag.
    """
    sign = "+" if score["severity"] > 0 else ""
    body_or_snippet = article.get("body") or article.get("snippet") or ""

    user_content = (
        f"TICKER:           {article.get('ticker') or '(none)'}\n"
        f"DOMAIN:           {article.get('domain') or '(unknown)'}\n"
        f"PUBLISHED:        {article.get('published') or '(unknown)'}\n"
        f"KEYWORDS_MATCHED: {article.get('keywords') or '(none)'}\n"
        f"HEADLINE:         {article.get('title') or ''}\n"
        f"BODY ({len(body_or_snippet)} chars):\n"
        f"---\n{body_or_snippet}\n---\n\n"
        f"PRECOMPUTED SCORE (from Python rule-engine):\n"
        f"  relevance      = {score['relevance']}\n"
        f"  stance         = {score['stance']}\n"
        f"  severity       = {sign}{score['severity']}  (raw {score.get('severity_raw')}, "
        f"recency_factor {score.get('recency_factor')}, age_days {score.get('age_days')})\n"
        f"  confidence     = {score['confidence']}\n\n"
        f"Extract event_date / infraction_type / summary / context, write the "
        f"rationale, and flag if you disagree with the score."
    )

    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": RATIONALE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        output_format=RationaleAndExtraction,
    )
    return response.parsed_output


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
