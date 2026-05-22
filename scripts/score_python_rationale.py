"""
v3 hybrid scoring: Python computes the numeric score (deterministic, recency-decayed
from article.published), Claude extracts structured context (event_date, infraction
type/summary/context_note) and writes the rationale.

For each article with one or more keyword hits:
    1. Load hits + taxonomy metadata (severity, stance per hit)
    2. score_python(...)  -> relevance/stance/severity/confidence + age_days + recency_factor
    3. write_rationale(client, ...)  -> RationaleAndExtraction (Pydantic)
    4. Persist to article_scores under prompt_version = 'risk-score-v3-py-rationale'
    5. Route low-confidence / high-severity / Claude-disagrees rows to review_queue

Idempotent: UNIQUE(article_id, model, prompt_version) means re-runs upsert.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ANTHROPIC_MODEL, RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.risk_scoring import (
    PROMPT_VERSION_V3_PY_CLAUDE_RATIONALE,
    get_client,
    refine_score_with_event_date,
    relative_date_label,
    score_python,
    write_rationale,
)

LOW_CONFIDENCE = 0.6
HIGH_SEVERITY = 4


def fetch_articles_with_hits(conn) -> list[dict]:
    """All articles with at least one keyword hit + their hits/text — both v3-scored and not."""
    rows = conn.execute(
        """
        SELECT
            a.id, a.ticker, a.company, a.published, a.source, a.domain,
            a.title, a.snippet,
            t.extracted_text  AS body,
            t.extracted_chars AS body_chars,
            t.fetch_source,
            EXISTS (SELECT 1 FROM companies c WHERE c.ticker = a.ticker AND c.status='active')
                AS ticker_in_watchlist,
            (SELECT GROUP_CONCAT(DISTINCT tax.category || ':' || tax.keyword)
             FROM article_keyword_hits h
             JOIN query_taxonomy tax ON tax.id = h.taxonomy_id
             WHERE h.article_id = a.id)
                AS keywords
        FROM articles a
        LEFT JOIN article_text t ON t.article_id = a.id AND t.fetch_status = 'success'
        WHERE EXISTS (SELECT 1 FROM article_keyword_hits h WHERE h.article_id = a.id)
        ORDER BY a.published DESC;
        """
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_hits_for_article(conn, article_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT tax.id, tax.category, tax.keyword, tax.severity, tax.stance
        FROM article_keyword_hits h
        JOIN query_taxonomy tax ON tax.id = h.taxonomy_id
        WHERE h.article_id = ? AND tax.active = 1;
        """,
        (article_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_v3_score(conn, article_id: int, score: dict, extraction) -> None:
    conn.execute(
        """
        INSERT INTO article_scores
            (article_id, model, prompt_version,
             relevance, stance, severity, severity_raw, confidence,
             rationale, event_date, infraction_type, infraction_summary,
             context_note, disagrees_with_score, disagreement_note,
             age_days, recency_factor, recency_source, scored_at_utc)
        VALUES (?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?)
        ON CONFLICT(article_id, model, prompt_version) DO UPDATE SET
            relevance            = excluded.relevance,
            stance               = excluded.stance,
            severity             = excluded.severity,
            severity_raw         = excluded.severity_raw,
            confidence           = excluded.confidence,
            rationale            = excluded.rationale,
            event_date           = excluded.event_date,
            infraction_type      = excluded.infraction_type,
            infraction_summary   = excluded.infraction_summary,
            context_note         = excluded.context_note,
            disagrees_with_score = excluded.disagrees_with_score,
            disagreement_note    = excluded.disagreement_note,
            age_days             = excluded.age_days,
            recency_factor       = excluded.recency_factor,
            recency_source       = excluded.recency_source,
            scored_at_utc        = excluded.scored_at_utc;
        """,
        (
            article_id, ANTHROPIC_MODEL, PROMPT_VERSION_V3_PY_CLAUDE_RATIONALE,
            score["relevance"], score["stance"], score["severity"],
            score["severity_raw"], score["confidence"],
            extraction.rationale, extraction.event_date,
            extraction.infraction_type, extraction.infraction_summary,
            extraction.context_note,
            1 if extraction.disagrees_with_score else 0,
            extraction.disagreement_note,
            score["age_days"], score["recency_factor"], score["recency_source"],
            utcnow(),
        ),
    )


def route_to_review(conn, article_id: int, score: dict, extraction) -> str | None:
    reasons: list[str] = []
    priority = 3
    if score["confidence"] < LOW_CONFIDENCE:
        reasons.append("low_confidence")
        priority = min(priority, 2)
    if abs(score["severity"]) >= HIGH_SEVERITY:
        reasons.append("high_severity")
        priority = 1
    if extraction.disagrees_with_score:
        reasons.append("claude_disagrees")
        priority = 1
    if score["relevance"] >= 50 and score["stance"] == "neutral":
        reasons.append("ambiguous_stance")
        priority = min(priority, 2)
    if not reasons:
        return None
    conn.execute(
        """
        INSERT OR IGNORE INTO review_queue
            (article_id, reason, priority, status, created_at_utc)
        VALUES (?, ?, ?, 'pending', ?);
        """,
        (article_id, "|".join(reasons), priority, utcnow()),
    )
    return "|".join(reasons)


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)
    client = get_client()

    articles = fetch_articles_with_hits(conn)
    print(f"Articles with keyword hits: {len(articles)}\n")

    for i, art in enumerate(articles, 1):
        hits = fetch_hits_for_article(conn, art["id"])
        if not hits:
            continue

        sym = f"[{art['ticker']}] " if art["ticker"] else ""
        watched = "*" if art.get("ticker_in_watchlist") else " "
        print(f"--- {i}/{len(articles)} ---")
        print(f"{watched}{sym}{art['title'][:100]}")
        print(f"  domain: {art['domain']}  published: {art['published']}  body_chars: {art.get('body_chars') or 0}")

        # 1. Provisional Python score (publish-date recency)
        provisional = score_python(art, hits)

        # 2. Claude extraction (gives us event_date)
        try:
            extraction = write_rationale(client, art, provisional)
        except Exception as e:
            print(f"  CLAUDE ERROR: {e}\n")
            continue

        # 3. Refine score using extracted event_date for recency
        py = refine_score_with_event_date(provisional, extraction.event_date)

        sign = "+" if py["severity"] > 0 else ""
        sign_raw = "+" if py["severity_raw"] > 0 else ""
        date_label = relative_date_label(py["age_days"])
        date_source = py["recency_source"]
        ev = extraction.event_date or "(no event date extracted)"
        print(f"  EVENT:   {ev}  ({date_label} via {date_source})")
        print(
            f"  python:  rel={py['relevance']:3d}  stance={py['stance']:24s}  "
            f"sev={sign}{py['severity']} (raw {sign_raw}{py['severity_raw']})  "
            f"conf={py['confidence']:.2f}  rf={py['recency_factor']:.2f}"
        )
        if provisional["severity"] != py["severity"]:
            print(f"  (decay refined sev {provisional['severity']:+d} -> {py['severity']:+d} after event_date)")

        # 4. Persist + review-queue routing
        insert_v3_score(conn, art["id"], py, extraction)
        reasons = route_to_review(conn, art["id"], py, extraction)
        conn.commit()

        flag = " [DISAGREES]" if extraction.disagrees_with_score else ""
        print(f"  infraction:   {extraction.infraction_type}")
        print(f"  summary:      {extraction.infraction_summary}")
        print(f"  context:      {extraction.context_note}")
        print(f"  rationale:    {extraction.rationale}{flag}")
        if extraction.disagrees_with_score:
            print(f"  DISAGREEMENT: {extraction.disagreement_note}")
        if reasons:
            print(f"  -> review_queue ({reasons})")
        print()

    # ── Summary table — sorted by event recency, dates prominent ──────
    print("=" * 110)
    print("v3 articles by EVENT recency (when did it actually happen?)\n")
    print(
        f"  {'TICKER':6s}  {'EVENT_DATE':12s}  {'AGE':>14s}  "
        f"{'sev':>6s}  {'rel':>4s}  {'INFRACTION_TYPE':22s}  TITLE"
    )
    for r in conn.execute(
        """
        SELECT a.ticker, a.title, a.published, a.domain,
               s.relevance, s.stance, s.severity, s.severity_raw, s.confidence,
               s.age_days, s.recency_factor, s.recency_source,
               s.event_date, s.infraction_type, s.disagrees_with_score
        FROM article_scores s
        JOIN articles a ON a.id = s.article_id
        WHERE s.model = ? AND s.prompt_version = ?
        ORDER BY
            CASE WHEN s.event_date IS NULL THEN 1 ELSE 0 END,
            s.event_date DESC,
            ABS(s.severity) DESC;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION_V3_PY_CLAUDE_RATIONALE),
    ):
        sym = f"[{r['ticker'] or '-'}]"
        sign = "+" if r["severity"] > 0 else ""
        ev = r["event_date"] or "—"
        age_label = relative_date_label(r["age_days"])
        src_tag = f"({r['recency_source']})" if r["recency_source"] else ""
        dis = " [D]" if r["disagrees_with_score"] else ""
        title_clip = (r["title"] or "")[:60]
        print(
            f"  {sym:6s}  {ev:12s}  {age_label:>10s}{src_tag:>4s}  "
            f"{sign}{r['severity']:>4d}  {r['relevance']:>4d}  "
            f"{(r['infraction_type'] or '-'):22s}  {title_clip}{dis}"
        )

    conn.close()


if __name__ == "__main__":
    main()
