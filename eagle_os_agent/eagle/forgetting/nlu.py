"""Natural-language instruction driven forgetting (赛题 §1-5).

Converts free-form NL into the structured forget API.  All heavy logic
(logical FORGETTING vs physical FORGOTTEN, IndexJob, evidence erasure) is
delegated to ``ForgettingService`` — this module is only the NLU front-door.
Deterministic regex-first, embedding-rank fallback second; no LLM required
so it runs on-device.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from eagle.db.orm import KnowledgeRecord, PreferenceRecord
from eagle.domain.enums import KnowledgeStatus, PreferenceStatus


@dataclass(frozen=True)
class NLForgetRequest:
    kind: str  # "P" | "K" | "BOTH"
    target_ids: tuple[str, ...]
    reason: str
    raw_instruction: str


# ---------------------------------------------------------------------------
# 1) intent detection
# ---------------------------------------------------------------------------

_FORGET_VERBS = r"(?:忘[掉得]?|删除|清除|移除|抹掉|forget|remove|delete|clear|erase)"
_PREF_HINTS = re.compile(r"(?:偏好|习惯|默认|风格|安全策略|preference|preferred|style|policy)", re.I)
_KNOW_HINTS = re.compile(r"(?:知识|工作流|workflow|案例|模板|fallback|知识库|knowledge)", re.I)
_WPS_HINTS = re.compile(r"(?:wps|libreoffice|office|docx|pdf|浏览器|browser)", re.I)

# time/window extraction (e.g. "上个月设置的", "last month")
_TIME_RE = re.compile(r"(?:上个月|上周|最近|上次|last\s+\w+|past\s+\w+)", re.I)


def _infer_kind(nl: str) -> str:
    has_p = bool(_PREF_HINTS.search(nl))
    has_k = bool(_KNOW_HINTS.search(nl) or _WPS_HINTS.search(nl))
    if has_p and has_k:
        return "BOTH"
    if has_p:
        return "P"
    if has_k:
        return "K"
    return "BOTH"


def _tokenize(nl: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。；;！!？?、]+", nl.strip().lower()) if t]


# ---------------------------------------------------------------------------
# 2) candidate memory ranking — uses LIKE containment + shim embed cosine
# ---------------------------------------------------------------------------

def _like_candidates(session, user_id: str, keywords: list[str], kind: str) -> list[tuple[str, float]]:
    scored: list[tuple[str, float]] = []
    if kind in ("P", "BOTH"):
        prefs = list(session.scalars(select(PreferenceRecord).where(
            PreferenceRecord.user_id == user_id,
            PreferenceRecord.status == PreferenceStatus.ACTIVE.value,
        )))
        for p in prefs:
            text = f"{p.preference_key} {p.preference_value_json} {p.scene_json}".lower()
            s = sum(1 for kw in keywords if kw and kw in text) / max(len(keywords), 1)
            if s > 0:
                scored.append((p.id, s + 0.05))
            elif not keywords:
                scored.append((p.id, 0.1))
    if kind in ("K", "BOTH"):
        knows = list(session.scalars(select(KnowledgeRecord).where(
            KnowledgeRecord.user_id == user_id,
            KnowledgeRecord.status == KnowledgeStatus.ACTIVE.value,
        )))
        for k in knows:
            text = f"{k.knowledge_type} {k.content_json} {k.retrieval_text} {k.scene_json}".lower()
            s = sum(1 for kw in keywords if kw and kw in text) / max(len(keywords), 1)
            if s > 0:
                scored.append((k.id, s))
            elif not keywords:
                scored.append((k.id, 0.1))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def parse_nl_forget(nl: str, user_id: str, session) -> NLForgetRequest:
    """Parse NL forget instruction → structured request.

    ``session`` is an open SQLAlchemy Session (caller manages tx).
    Raises ValueError if no forget intent detected.
    """
    if not re.search(_FORGET_VERBS, nl, re.I):
        raise ValueError("No forget intent detected in instruction")
    kind = _infer_kind(nl)
    # extract keywords: strip verbs/stopwords, keep content tokens >1 char
    stop = {"的","了","把","被","将","请","帮我","一下","掉","得","a","the","please","my","me"}
    toks = [t for t in _tokenize(nl) if t not in stop and len(t) > 1 and not re.fullmatch(_FORGET_VERBS, t, re.I)]
    # also split CJK n-grams for matching: add 2-char windows for CJK
    extra: list[str] = []
    for tok in list(toks):
        if any("\u4e00" <= c <= "\u9fff" for c in tok) and len(tok) > 2:
            extra.extend(tok[i:i+2] for i in range(len(tok)-1))
    keywords = toks + extra
    # also search for explicit tool/filetype tokens
    for m in re.finditer(r"[a-zA-Z]{2,}", nl.lower()):
        if m.group(0) not in keywords:
            keywords.append(m.group(0))

    scored = _like_candidates(session, user_id, keywords, kind)
    # threshold: keep items with score>0, cap 5
    target_ids = tuple(s for s, _ in scored[:5] if _ > 0) if scored else ()
    # also expose raw ids list for caller to map kind
    ids = tuple(i for i, _ in scored[:5]) if scored else ()
    # filter ids by actual overlap if keywords empty -> return all active of inferred kind (capped)
    if not ids and not keywords:
        ids = tuple(i for i, _ in scored[:5])
    return NLForgetRequest(kind=kind, target_ids=ids, reason=f"NL:{nl[:80]}", raw_instruction=nl)


def resolve_targets(nl: str, user_id: str, session) -> dict[str, list[str]]:
    """Return {kind -> ids} split by actual table membership."""
    req = parse_nl_forget(nl, user_id, session)
    pref_ids = set(r[0] for r in session.execute(select(PreferenceRecord.id).where(PreferenceRecord.user_id == user_id)).all()) if req.target_ids else set()
    # simpler: query each
    p_ids: list[str] = []
    k_ids: list[str] = []
    for mid in req.target_ids:
        if session.get(PreferenceRecord, mid) is not None:
            p_ids.append(mid)
        elif session.get(KnowledgeRecord, mid) is not None:
            k_ids.append(mid)
    if req.kind == "P":
        return {"P": p_ids, "K": []}
    if req.kind == "K":
        return {"P": [], "K": k_ids}
    return {"P": p_ids, "K": k_ids}
