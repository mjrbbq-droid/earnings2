"""
Score articles in institutional_risk.db that have keyword hits but no Claude score yet.

For each:
    article (headline + snippet + ticker context + matched keywords)
      -> Claude (Opus 4.7, adaptive thinking, structured output)
      -> article_scores  (relevance, stance, severity, confidence, rationale)
      -> review_queue    (if confidence < 0.6, |severity| >= 4, or ambiguous)

Idempotent: UNIQUE(article_id, model, prompt_version) means re-runs upsert.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ANTHROPIC_MODEL, RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.risk_scoring import PROMPT_VERSION, get_client, score_article

LOW_CONFIDENCE = 0.6
HIGH_SEVERITY = 4


def fetch_articles_to_score(conn) -> list[dict]:
    """Articles that have a keyword hit but no score for this model+prompt_version yet."""
    rows = conn.execute(
        """
        SELECT
            a.id,
            a.ticker,
            a.published,
            a.source,
            a.title,
            a.snippet,
            GROUP_CONCAT(t.category || ':' || t.keyword, '; ') AS keywords
        FROM articles a
        JOIN article_keyword_hits h ON h.article_id = a.id
        JOIN query_taxonomy t       ON t.id         = h.taxonomy_id
        LEFT JOIN article_scores s
               ON s.article_id     = a.id
              AND s.model          = ?
              AND s.prompt_version = ?
        WHERE s.id IS NULL
        GROUP BY a.id
        ORDER BY a.published DESC;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_score(conn, article_id: int, score) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO article_scores
            (article_id, model, prompt_version,
             relevance, stance, severity, confidence, rationale, scored_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            article_id,
            ANTHROPIC_MODEL,
            PROMPT_VERSION,
            score.relevance,
            score.stance,
            score.severity,
            score.confidence,
            score.rationale,
            utcnow(),
        ),
    )


def route_to_review(conn, article_id: int, score) -> str | None:
    """Insert into review_queue if the score triggers a review condition."""
    reasons: list[str] = []
    priority = 3

    if score.confidence < LOW_CONFIDENCE:
        reasons.append("low_confidence")
        priority = min(priority, 2)
    if abs(score.severity) >= HIGH_SEVERITY:
        reasons.append("high_severity")
        priority = 1
    if score.relevance >= 50 and score.stance == "neutral":
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

    articles = fetch_articles_to_score(conn)
    print(f"Articles needing scoring: {len(articles)}  (model={ANTHROPIC_MODEL}, prompt={PROMPT_VERSION})\n")

    if not articles:
        print("Nothing to score. Run scripts/ingest_fmp_news.py first.")
        return

    for i, art in enumerate(articles, 1):
        sym = f"[{art['ticker']}] " if art["ticker"] else ""
        print(f"--- {i}/{len(articles)} ---")
        print(f"{sym}{art['title'][:100]}")
        print(f"  keys: {art['keywords']}")

        try:
            score = score_article(client, art)
        except Exception as e:
            print(f"  ERROR: {e}\n")
            continue

        insert_score(conn, art["id"], score)
        reasons = route_to_review(conn, art["id"], score)
        conn.commit()

        sign = "+" if score.severity > 0 else ""
        print(
            f"  relevance={score.relevance:3d}  stance={score.stance:24s}  "
            f"severity={sign}{score.severity}  conf={score.confidence:.2f}"
        )
        if reasons:
            print(f"  -> review_queue ({reasons})")
        print(f"  rationale: {score.rationale}\n")

    # ── Summary ──────────────────────────────────────────────────────────
    print("=" * 70)
    print("SUMMARY\n")

    print("--- stance distribution ---")
    for r in conn.execute(
        """
        SELECT stance, COUNT(*) AS n, AVG(relevance) AS avg_rel, AVG(severity) AS avg_sev
        FROM article_scores
        WHERE model = ? AND prompt_version = ?
        GROUP BY stance
        ORDER BY n DESC;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION),
    ):
        print(f"  {r['stance']:24s}  n={r['n']:3d}  avg_relevance={r['avg_rel']:.0f}  avg_severity={r['avg_sev']:+.1f}")

    print("\n--- review queue ---")
    for r in conn.execute(
        """
        SELECT q.priority, q.reason, a.ticker, a.title, s.relevance, s.severity, s.confidence
        FROM review_queue q
        JOIN articles a       ON a.id         = q.article_id
        JOIN article_scores s ON s.article_id = q.article_id
                              AND s.model     = ?
                              AND s.prompt_version = ?
        WHERE q.status = 'pending'
        ORDER BY q.priority, s.severity DESC;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION),
    ):
        sym = f"[{r['ticker']}] " if r['ticker'] else ""
        sign = "+" if r['severity'] > 0 else ""
        print(f"  P{r['priority']}  {r['reason']:35s}  {sym}rel={r['relevance']} sev={sign}{r['severity']} conf={r['confidence']:.2f}")
        print(f"      {r['title'][:100]}")

    conn.close()


if __name__ == "__main__":
    main()
