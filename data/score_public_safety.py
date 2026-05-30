"""
Public-safety engagement scoring + quadrant map.

Reads police_policy_stance_20260528.xlsx, produces two INDEPENDENT scores per
company (Reform-alignment, Public-safety-alignment), writes a scored workbook
with an editable weights/assumptions sheet, and renders a quadrant scatter
(interactive HTML + static PNG) that positions rather than ranks companies.
"""
import glob
import math
import os
from pathlib import Path

import pandas as pd
import numpy as np

# Resolve paths relative to this script's directory (data/), and auto-pick the
# most recent stance export so re-runs always score the latest data.
os.chdir(Path(__file__).resolve().parent)
SRC = max(glob.glob("police_policy_stance_*.xlsx"), key=os.path.getmtime)
_stamp = SRC.replace("police_policy_stance_", "").replace(".xlsx", "")
OUT_XLSX = f"police_policy_scored_{_stamp}.xlsx"
OUT_HTML = "public_safety_quadrant.html"
OUT_PNG = "public_safety_quadrant.png"
print(f"Scoring source: {SRC}")

# --- ASSUMPTIONS: action-type materiality weights (1-5). Edit these. ---------
REFORM_WEIGHTS = {
    # Adversarial-to-police (highest reform signal per the taxonomy)
    "defund_police_explicit": 5,
    "donations_anti_police_adversarial": 5,   # funds BLM Found, ArchCity, M4BL, etc.
    "exit_facial_recognition_police": 4,
    "severed_police_partnership": 4,
    "moratorium_facial_recognition_police": 3,
    "refuse_facial_recognition_police": 3,
    "boycott_police_products": 3,
    # Criminal-justice-reform-adjacent (lower weight — not adversarial)
    "donations_broad_criminal_justice_reform": 3,
    "donations_collaborative_reform": 2,       # works WITH police (Policing Project @ NYU)
    "donations_innocence_wrongful_conviction": 2,
    "reform_advocacy_grants": 2,
    "donations_reentry_employment": 1,         # post-incarceration employment
}
ENFORCE_WEIGHTS = {
    "manufactures_police_weapons": 5,
    "sells_surveillance_tech_to_police": 5,
    "sells_data_analytics_to_police": 4,
    "hosts_police_data_infrastructure": 4,
    "donates_to_police_foundations": 3,
    "donates_to_police_unions": 3,
    "sells_radios_comms_to_police": 2,
    "public_statement_supporting_police": 2,
    "marketing_partnership_first_responders": 1,
}
LABELS = {
    "reform_leaning": "Reform-aligned",
    "enforcement_leaning": "Public-safety-aligned",
    "cross_exposure": "Mixed engagement",
    "no_material_exposure": "No material position",
}
MAX_RAW = 5 * 0.99  # weight x max confidence, for 0-100 scaling


def scale(raw):
    return round(100 * raw / MAX_RAW, 1)


def main():
    comp = pd.read_excel(SRC, sheet_name="All Companies")
    comp["Confidence"] = pd.to_numeric(comp["Confidence"], errors="coerce").fillna(0.0)

    # --- two independent alignment scores: action weight x confidence --------
    rw = comp["Anti type"].map(REFORM_WEIGHTS).fillna(0.0)
    ew = comp["Pro type"].map(ENFORCE_WEIGHTS).fillna(0.0)
    comp["Reform-alignment"] = (rw * comp["Confidence"]).map(scale)
    comp["Public-safety-alignment"] = (ew * comp["Confidence"]).map(scale)

    # --- dollar evidence (drives bubble size; not the score) -----------------
    le = pd.read_excel(SRC, sheet_name="Federal LE Contracts")
    le["Award Amount"] = pd.to_numeric(le["Award Amount"], errors="coerce").fillna(0)
    le_dollars = le.groupby("Ticker")["Award Amount"].sum()

    sch = pd.read_excel(SRC, sheet_name="Schedule I detail (990)")
    sch["Grant amount"] = pd.to_numeric(sch["Grant amount"], errors="coerce").fillna(0)
    gr_dollars = sch.groupby("Donor ticker")["Grant amount"].sum()

    comp["LE contract $"] = comp["Ticker"].map(le_dollars).fillna(0)
    comp["Grant $"] = comp["Ticker"].map(gr_dollars).fillna(0)
    comp["Dollar evidence"] = comp["LE contract $"] + comp["Grant $"]

    comp["Category"] = comp["Net Position"].map(LABELS).fillna(comp["Net Position"])

    scores = comp[[
        "Ticker", "Company", "Sector", "Category",
        "Reform-alignment", "Public-safety-alignment",
        "Confidence", "Dollar evidence", "LE contract $", "Grant $",
        "Anti type", "Pro type",
    ]].sort_values(
        ["Public-safety-alignment", "Reform-alignment"], ascending=False
    ).reset_index(drop=True)

    # --- write scored workbook with editable assumptions sheet ---------------
    rows = ([{"Side": "Reform", "Action type": k, "Weight (1-5)": v} for k, v in REFORM_WEIGHTS.items()]
            + [{"Side": "Public-safety", "Action type": k, "Weight (1-5)": v} for k, v in ENFORCE_WEIGHTS.items()])
    assumptions = pd.DataFrame(rows)
    scope = pd.DataFrame({"Note": [
        "Scope: maps publicly disclosed corporate positions, philanthropy, and "
        "commercial relationships relating to law enforcement and the broader "
        "public safety sector.",
        "Classifications reflect observable activity and disclosure, not an "
        "assessment of any company's values or intent.",
        "Underlying evidence is primarily drawn from law-enforcement-related sources.",
        "Score = action-type weight x confidence, scaled 0-100. The two axes are "
        "independent (a company can score on both).",
        "Edit the Weights sheet and re-run score_public_safety.py to recompute.",
    ]})
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        scope.to_excel(xw, sheet_name="Scope", index=False)
        assumptions.to_excel(xw, sheet_name="Weights", index=False)
        scores.to_excel(xw, sheet_name="Company Scores", index=False)

    # --- quadrant scatter ----------------------------------------------------
    import plotly.graph_objects as go
    plot = scores.copy()
    # jitter so stacked zero-points are visible
    rng = np.random.default_rng(7)
    plot["x"] = plot["Reform-alignment"] + rng.uniform(-1.5, 1.5, len(plot))
    plot["y"] = plot["Public-safety-alignment"] + rng.uniform(-1.5, 1.5, len(plot))
    # Bubble size: log-scaled by dollar evidence, capped so mega-cap LE
    # contractors don't swallow neighbors (old formula maxed ~120; this caps ~22).
    plot["size"] = (4 + plot["Dollar evidence"].apply(
        lambda d: 4 * math.log10(d) if d > 0 else 0)).clip(lower=4, upper=22)

    color_map = {
        "Reform-aligned": "#2b8cbe",
        "Public-safety-aligned": "#d95f0e",
        "Mixed engagement": "#756bb1",
        "No material position": "#cccccc",
    }
    # Tickers to label directly on the chart: the screen pool (reform + mixed —
    # the deliverable) plus the top public-safety outliers for context. Hover
    # still shows full info for every point.
    label_set = set(plot.loc[plot["Category"].isin(
        ["Reform-aligned", "Mixed engagement"]), "Ticker"])
    label_set |= set(plot.sort_values("Public-safety-alignment", ascending=False)
                          .head(5)["Ticker"])
    fig = go.Figure()
    for cat, sub in plot.groupby("Category"):
        engaged = cat != "No material position"
        fig.add_trace(go.Scatter(
            x=sub["x"], y=sub["y"], mode="markers", name=cat,
            marker=dict(size=sub["size"].clip(lower=6), color=color_map.get(cat, "#999"),
                        opacity=(0.85 if engaged else 0.25),
                        line=dict(width=0.5, color="white")),
            text=sub.apply(lambda r: f"{r['Company']} ({r['Ticker']})<br>{r['Sector']}<br>"
                                     f"Reform {r['Reform-alignment']} | Public-safety {r['Public-safety-alignment']}<br>"
                                     f"Confidence {r['Confidence']} | $ evidence {r['Dollar evidence']:,.0f}", axis=1),
            hoverinfo="text",
        ))
    # Ticker labels for the screen pool + top-5 PS outliers
    lbl = plot[plot["Ticker"].isin(label_set)]
    fig.add_trace(go.Scatter(
        x=lbl["x"], y=lbl["y"], mode="text", text=lbl["Ticker"],
        textposition="top right", textfont=dict(size=10, color="#222"),
        hoverinfo="skip", showlegend=False,
    ))
    mid = 50
    fig.add_hline(y=mid, line=dict(color="#ddd", width=1))
    fig.add_vline(x=mid, line=dict(color="#ddd", width=1))
    ann = dict(showarrow=False, font=dict(size=12, color="#888"))
    fig.add_annotation(x=12, y=95, text="Public-safety-aligned", **ann)
    fig.add_annotation(x=88, y=95, text="Mixed engagement", **ann)
    fig.add_annotation(x=12, y=5, text="Limited exposure", **ann)
    fig.add_annotation(x=85, y=5, text="Reform-forward", **ann)
    fig.update_layout(
        title="Public Safety Engagement Profile — Russell 1000",
        xaxis=dict(title="Reform-alignment score", range=[-5, 105]),
        yaxis=dict(title="Public-safety-alignment score", range=[-5, 105]),
        template="plotly_white", width=1000, height=750,
        legend=dict(title="Category"),
    )
    fig.write_html(OUT_HTML)

    # static PNG via matplotlib (no kaleido dependency)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig2, ax = plt.subplots(figsize=(11, 8.5))
    for cat, sub in plot.groupby("Category"):
        engaged = cat != "No material position"
        ax.scatter(sub["x"], sub["y"], s=(sub["size"] * 5),
                   c=color_map.get(cat, "#999"), alpha=(0.85 if engaged else 0.20),
                   edgecolors="white", linewidths=0.4, label=cat)
    # Ticker labels for the screen pool + top-5 PS outliers
    for _, r in plot[plot["Ticker"].isin(label_set)].iterrows():
        ax.text(r["x"] + 1.0, r["y"] + 1.0, r["Ticker"], fontsize=7,
                color="#222", ha="left", va="bottom", clip_on=True)
    ax.axhline(mid, color="#ddd"); ax.axvline(mid, color="#ddd")
    for x, y, t in [(8, 96, "Public-safety-aligned"), (78, 96, "Mixed engagement"),
                    (8, 3, "Limited exposure"), (80, 3, "Reform-forward")]:
        ax.text(x, y, t, color="#999", fontsize=11)
    ax.set_xlim(-5, 105); ax.set_ylim(-5, 105)
    ax.set_xlabel("Reform-alignment score"); ax.set_ylabel("Public-safety-alignment score")
    ax.set_title("Public Safety Engagement Profile — Russell 1000")
    ax.legend(title="Category", loc="center right", framealpha=0.9)
    fig2.tight_layout(); fig2.savefig(OUT_PNG, dpi=130)

    # console summary
    print("Wrote:", OUT_XLSX, "|", OUT_HTML, "|", OUT_PNG)
    print("\nCategory counts:")
    print(scores["Category"].value_counts().to_string())
    print("\nTop 10 by Public-safety-alignment:")
    print(scores.head(10)[["Ticker", "Company", "Sector", "Category",
                           "Reform-alignment", "Public-safety-alignment",
                           "Dollar evidence"]].to_string(index=False))
    rf = scores[scores["Reform-alignment"] > 0].sort_values("Reform-alignment", ascending=False)
    print("\nReform-aligned companies:")
    print(rf[["Ticker", "Company", "Reform-alignment", "Public-safety-alignment"]].to_string(index=False))


if __name__ == "__main__":
    main()
