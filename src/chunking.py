# src/chunking.py
from __future__ import annotations

import re
from typing import List, Tuple


# ---------------------------
# Q&A split detection
# ---------------------------

QA_HEADER_LINE_RE = re.compile(
    r"^\s*(question(?:s)?\s*(?:-|and)\s*answer(?:s)?\s*(?:section)?|q\s*&\s*a)\s*\.?\s*$",
    re.IGNORECASE,
)

QA_OPERATOR_CUE_RE = re.compile(
    r"^\s*operator\s*:\s*.*("
    r"we will now begin (the )?question[- ]and[- ]answer|"
    r"question[- ]and[- ]answer session|"
    r"our next question comes from|"
    r"we will now take questions|"
    r"we will now open the line for questions|"
    r"we will take your questions"
    r").*$",
    re.IGNORECASE,
)

QA_INLINE_CUE_RE = re.compile(
    r"\b(we will now begin (the )?question[- ]and[- ]answer|question[- ]and[- ]answer session)\b",
    re.IGNORECASE,
)


def split_prepared_vs_qa(text: str) -> tuple[str, str]:
    """
    Split the transcript into prepared remarks and Q&A.
    Uses strong line-based cues; avoids splitting on early header "Q&A." noise.
    """
    if not text or not text.strip():
        return "", ""

    lines = text.splitlines()

    offsets: List[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1  # newline

    # 1) Best: operator cue line
    for i, ln in enumerate(lines):
        if QA_OPERATOR_CUE_RE.match(ln):
            cut = offsets[i]
            return text[:cut].strip(), text[cut:].strip()

    # 2) Explicit Q&A header line, but ignore early header noise
    for i, ln in enumerate(lines):
        if QA_HEADER_LINE_RE.match(ln):
            if i < 25:
                continue
            cut = offsets[i]
            return text[:cut].strip(), text[cut:].strip()

    # 3) Fallback: inline cue anywhere
    m = QA_INLINE_CUE_RE.search(text)
    if m:
        cut = m.start()
        return text[:cut].strip(), text[cut:].strip()

    return text.strip(), ""


# ---------------------------
# Speaker chunking
# ---------------------------

# Standard speaker line: "Speaker: text"
SPEAKER_COLON_RE = re.compile(r"^\s*([A-Z][A-Za-z\.\-'\s]{2,60})\s*:\s*(.+?)\s*$")

# FactSet style header: "Name - Title" (speech starts on following lines)
# NOTE: We will only accept this as a speaker header if the RHS looks like a real title.
SPEAKER_DASH_RE = re.compile(
    r"^\s*([A-Z][A-Za-z0-9\.\-'\s]{1,80})\s*[-–]\s*([A-Za-z].{1,140})\s*$"
)

# RHS title must contain role/company hints, otherwise it is probably just a wrapped sentence (e.g. "Phoenix ... – there was a ...")
TITLE_HINT_RE = re.compile(
    r"\b("
    r"analyst|"
    r"chief|officer|"
    r"president|chairman|director|"
    r"vice|svp|evp|"
    r"cfo|ceo|"
    r"investor relations|"
    r"inc\.|corp\.|corporation|llc|ltd|co\."
    r")\b",
    re.IGNORECASE,
)

# Junk tokens that should never become speakers
BAD_SPEAKER_TOKENS = {"TOTAL PAGES", "Q&A", "QA", "PRESENTATION"}

# Reject “speakers” that are actually header lines
BAD_SPEAKER_PREFIX_RE = re.compile(
    r"^(Q\d\b|Q\d\s*\d{4}\b|"
    r"earnings\s+call\b|"
    r"total\b|presentation\b|"
    r"forward[- ]looking\b|any\s+forward\b)",
    re.IGNORECASE,
)


def chunk_by_speaker_lines(section_text: str, default_speaker: str = "UNKNOWN") -> List[Tuple[str, str]]:
    """
    Turn a prepared/Q&A block into (speaker, text) chunks.

    Key protections:
      - Only accept "Name - Title" headers when Title looks like a real role/company.
      - Prevent wrapped sentences with dashes (Phoenix ... – there was ...) from being treated as speakers.
      - Keep header/junk lines as content, not as speakers.
    """
    if not section_text or not section_text.strip():
        return []

    lines = section_text.splitlines()
    chunks: List[Tuple[str, List[str]]] = []

    cur_speaker = default_speaker
    cur_buf: List[str] = []
    found_any = False

    def flush():
        nonlocal cur_buf
        if cur_buf:
            chunks.append((cur_speaker, cur_buf))
            cur_buf = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # 1) Colon speaker lines: "Operator: ..."
        m = SPEAKER_COLON_RE.match(line)
        if m:
            spk = m.group(1).strip()

            # Reject junk “speakers”
            if spk.upper() in BAD_SPEAKER_TOKENS or BAD_SPEAKER_PREFIX_RE.match(spk):
                cur_buf.append(line)
                continue

            found_any = True
            flush()
            cur_speaker = spk
            cur_buf = [m.group(2).strip()]
            continue

        # 2) Dash speaker header lines: "Name - Title"
        m2 = SPEAKER_DASH_RE.match(line)
        if m2:
            spk = m2.group(1).strip()
            title = m2.group(2).strip()

            # Reject if RHS doesn't look like a title (prevents false speaker splits on sentence dashes)
            if not TITLE_HINT_RE.search(title):
                cur_buf.append(line)
                continue

            # Reject obvious header lines
            if spk.upper() in BAD_SPEAKER_TOKENS or BAD_SPEAKER_PREFIX_RE.match(spk):
                cur_buf.append(line)
                continue

            found_any = True
            flush()
            cur_speaker = spk
            # Do not treat title line as content; speech usually starts after it
            cur_buf = []
            continue

        # Normal content line
        cur_buf.append(line)

    flush()

    if not found_any:
        return [(default_speaker, section_text.strip())]

    out: List[Tuple[str, str]] = []
    for spk, buf in chunks:
        joined = " ".join([b for b in buf if b]).strip()
        if joined:
            out.append((spk, joined))
    return out
