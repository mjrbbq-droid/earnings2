"""
Re-score articles that have both a v1 (snippet) score AND a successful
full-text fetch, using the v2 fulltext prompt. Writes a new row in
article_scores under prompt_version = 'risk-score-v2-fulltext' — v1 stays.

Then prints a side-by-side comparison so you can see how the full body
changes the call.

Idempotent: UNIQUE(article_id, model, prompt_version) — re-running upserts.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ANTHROPIC_MODEL, RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.risk_scoring import (
    PROMPT_VERSION_V1_SNIPPET,
    PROMPT_VERSION_V2_FULLTEXT,
    get_client,
    score_article_fulltext,
)


def articles_to_rescore(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            a.id,
            a.ticker,
            a.published,
            a.source,
            a.domain,
            a.title,
            a.snippet,
            t.extracted_text  AS body,
            t.fetch_source,
            t.extracted_chars,
            GROUP_CONCAT(DISTINCT tax.category || ':' || tax.keyword) AS keywords
        FROM articles a
        JOIN article_text   t   ON t.article_id   = a.id   AND t.fetch_status = 'success'
        JOIN article_scores s1  ON s1.article_id  = a.id
                                AND s1.model      = ?
                                AND s1.prompt_version = ?
        LEFT JOIN article_keyword_hits h  ON h.article_id  = a.id
        LEFT JOIN query_taxonomy        tax ON tax.id      = h.taxonomy_id
        LEFT JOIN article_scores s2  ON s2.article_id  = a.id
                                     AND s2.model      = ?
                                     AND s2.prompt_version = ?
        WHERE s2.id IS NULL
        GROUP BY a.id
        ORDER BY ABS(s1.severity) DESC, s1.relevance DESC;
        """,
        (
            ANTHROPIC_MODEL, PROMPT_VERSION_V1_SNIPPET,
            ANTHROPIC_MODEL, PROMPT_VERSION_V2_FULLTEXT,
        ),
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
            PROMPT_VERSION_V2_FULLTEXT,
            score.relevance,
            score.stance,
            score.severity,
            score.confidence,
            score.rationale,
            utcnow(),
        ),
    )


def fetch_comparison(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            a.id, a.ticker, a.title, a.domain,
            t.fetch_source, t.extracted_chars,
            s1.relevance AS v1_rel, s1.stance AS v1_stance, s1.severity AS v1_sev,
            s1.confidence AS v1_conf, s1.rationale AS v1_rat,
            s2.relevance AS v2_rel, s2.stance AS v2_stance, s2.severity AS v2_sev,
            s2.confidence AS v2_conf, s2.rationale AS v2_rat
        FROM articles a
        JOIN article_text   t  ON t.article_id  = a.id AND t.fetch_status = 'success'
        JOIN article_scores s1 ON s1.article_id = a.id
                              AND s1.model      = ? AND s1.prompt_version = ?
        JOIN article_scores s2 ON s2.article_id = a.id
                              AND s2.model      = ? AND s2.prompt_version = ?
        ORDER BY ABS(s2.severity) DESC, s2.relevance DESC;
        """,
        (
            ANTHROPIC_MODEL, PROMPT_VERSION_V1_SNIPPET,
            ANTHROPIC_MODEL, PROMPT_VERSION_V2_FULLTEXT,
        ),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)
    client = get_client()

    to_score = articles_to_rescore(conn)
    print(f"Articles needing v2 rescoring: {len(to_score)}\n")

    for i, art in enumerate(to_score, 1):
        sym = f"[{art['ticker']}] " if art["ticker"] else ""
        print(f"--- {i}/{len(to_score)} ---")
        print(f"{sym}{art['title'][:100]}")
        print(f"  body: {art['extracted_chars']} chars via {art['fetch_source']}")
        try:
            score = score_article_fulltext(client, art)
        except Exception as e:
            print(f"  ERROR: {e}\n")
            continue
        insert_score(conn, art["id"], score)
        conn.commit()
        sign = "+" if score.severity > 0 else ""
        print(
            f"  v2: relevance={score.relevance:3d}  stance={score.stance:24s}  "
            f"severity={sign}{score.severity}  conf={score.confidence:.2f}"
        )
        print(f"  rationale: {score.rationale}\n")

    # ── Comparison ──────────────────────────────────────────────────────
    rows = fetch_comparison(conn)
    if not rows:
        print("No v1/v2 pairs to compare yet.")
        return

    print("=" * 110)
    print("v1 (snippet)  vs  v2 (fulltext)\n")
    print(
        f"  {'TICKER':6s}  "
        f"{'rel':>9s}  {'stance':>30s}  {'sev':>6s}  {'conf':>10s}  "
        f"{'fetch':>8s}  title"
    )
    for r in rows:
        sym = f"{r['ticker'] or '-':6s}"
        rel_diff = r["v2_rel"] - r["v1_rel"]
        sev_diff = r["v2_sev"] - r["v1_sev"]
        rel_str = f"{r['v1_rel']:>3d}->{r['v2_rel']:<3d}"
        stance_changed = "*" if r["v1_stance"] != r["v2_stance"] else " "
        stance_str = f"{r['v1_stance'][:10]:>10s}->{r['v2_stance'][:10]:<10s}{stance_changed}"
        sev_str = f"{r['v1_sev']:+d}->{r['v2_sev']:+d}"
        conf_str = f"{r['v1_conf']:.2f}->{r['v2_conf']:.2f}"
        print(
            f"  {sym}  "
            f"{rel_str:>9s}  {stance_str:>30s}  {sev_str:>6s}  {conf_str:>10s}  "
            f"{r['fetch_source']:>8s}  {r['title'][:60]}"
        )

    print()
    print("--- v2 rationales (full body) ---")
    for r in rows:
        sym = f"[{r['ticker']}] " if r['ticker'] else ""
        sign = "+" if r['v2_sev'] > 0 else ""
        print(f"\n{sym}{r['title'][:90]}")
        print(f"  v2 score: rel={r['v2_rel']} stance={r['v2_stance']} sev={sign}{r['v2_sev']} conf={r['v2_conf']:.2f}")
        print(f"  v1 said:  {r['v1_rat']}")
        print(f"  v2 says:  {r['v2_rat']}")

    conn.close()


if __name__ == "__main__":
    main()
