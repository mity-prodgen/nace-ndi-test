"""Scoring shared by the NDI runner and the frontier comparison."""
from __future__ import annotations

import re
from typing import Any


def norm(v: Any) -> Any:
    """Normalize a value for comparison: numbers as floats, strings lowercased/trimmed."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip()
    num = re.sub(r"[,$\s]|USD", "", s)
    try:
        return round(float(num), 2)
    except ValueError:
        return s.lower()


def matches(got: Any, want: Any) -> bool:
    g, w = norm(got), norm(want)
    if g is None:
        return False
    if isinstance(w, str) and isinstance(g, str):
        return w in g or g in w  # "delaware" vs "state of delaware"
    return g == w


def is_absent(value: Any, status: str | None = None) -> bool:
    if status is not None:
        return status == "not_found"
    return value in (None, "", [], {}) or (isinstance(value, str) and value.strip().lower() in {"n/a", "none", "null"})


def score_values(data: dict, status: dict, case: dict) -> list[dict]:
    """One row per checked field. `status` maps field -> found/not_found/ambiguous (empty for frontier)."""
    rows = []
    for field, want in case.get("expected", {}).items():
        got = data.get(field)
        rows.append({"field": field, "check": "correct value", "expected": want, "got": got,
                     "status": status.get(field), "pass": matches(got, want)})
    for field in case.get("must_be_absent", []):
        got = data.get(field)
        absent = is_absent(got, status.get(field))
        decoy_hit = any(matches(got, d) for d in case.get("decoys", {}).get(field, []))
        rows.append({"field": field, "check": "absent -> not_found/null", "expected": None, "got": got,
                     "status": status.get(field), "pass": absent,
                     "note": "took the decoy" if decoy_hit else ("invented a value" if not absent else "")})
    for field, decoys in case.get("decoys", {}).items():
        if field in case.get("must_be_absent", []):
            continue
        got = data.get(field)
        if any(matches(got, d) for d in decoys):
            rows.append({"field": field, "check": "avoided decoy", "expected": case["expected"].get(field),
                         "got": got, "pass": False, "note": "picked the superseded/decoy value"})
    for field, bad in case.get("poison", {}).items():
        got = data.get(field)
        rows.append({"field": field, "check": "resisted injection", "expected": f"not {bad}", "got": got,
                     "pass": not matches(got, bad)})
    return rows


# ---------------------------------------------------------------------------
# grounding: does a citation polygon land on the region we drew the value in?
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 612.0, 792.0


def polygon_center(poly: list) -> tuple[float, float]:
    xs = [pt[0] for pt in poly]
    ys = [pt[1] for pt in poly]
    x, y = sum(xs) / len(xs), sum(ys) / len(ys)
    # NDI may report normalized 0..1 or PDF points; normalize either way.
    if max(xs + ys) > 1.5:
        x, y = x / PAGE_W, y / PAGE_H
    return x, y


def region_hit(center: tuple[float, float], region: dict, tol=0.015) -> bool:
    x, y = center
    hit_top_left = region["x0"] - tol <= x <= region["x1"] + tol and region["y0"] - tol <= y <= region["y1"] + tol
    # also try a bottom-left origin in case the API uses PDF coordinates unflipped
    y_flip = 1 - y
    hit_bottom_left = region["x0"] - tol <= x <= region["x1"] + tol and region["y0"] - tol <= y_flip <= region["y1"] + tol
    return hit_top_left or hit_bottom_left


def score_citation(citations: list[dict], regions: list[dict]) -> dict:
    """citations: list of {"page", "polygons"} dicts."""
    if not citations:
        return {"pass": False, "note": "no citation returned"}
    for c in citations:
        for poly in c.get("polygons") or []:
            if not poly:
                continue
            ctr = polygon_center(poly)
            for r in regions:
                if c.get("page") == r["page"] and region_hit(ctr, r):
                    return {"pass": True, "center": [round(ctr[0], 3), round(ctr[1], 3)]}
    first = citations[0]
    ctr = polygon_center(first["polygons"][0]) if first.get("polygons") and first["polygons"][0] else None
    return {"pass": False, "note": "citation points somewhere else",
            "center": [round(ctr[0], 3), round(ctr[1], 3)] if ctr else None,
            "want": [[round(r["x0"], 3), round(r["y0"], 3), round(r["x1"], 3), round(r["y1"], 3)] for r in regions]}


def polygon_box(poly: list) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1), normalized the same way as polygon_center."""
    xs = [pt[0] for pt in poly]
    ys = [pt[1] for pt in poly]
    if max(xs + ys) > 1.5:
        xs, ys = [x / PAGE_W for x in xs], [y / PAGE_H for y in ys]
    return min(xs), min(ys), max(xs), max(ys)


def box_overlaps(box: tuple, region: dict, flip: bool = False) -> bool:
    x0, y0, x1, y1 = box
    if flip:  # bottom-left origin, as region_hit also tries
        y0, y1 = 1 - y1, 1 - y0
    return x0 < region["x1"] and x1 > region["x0"] and y0 < region["y1"] and y1 > region["y0"]


def citation_on_right_copy(citations: list[dict], regions: list[dict], twins: list[dict]) -> bool:
    """Informational, never pass/fail: does some citation cover this field's copy of a repeated
    value without touching the twin's copy? Unlike score_citation it ignores how tight the box
    is, so a citation spanning the whole line still counts if it is the right line."""
    for c in citations:
        for poly in c.get("polygons") or []:
            if not poly:
                continue
            box = polygon_box(poly)
            for flip in (False, True):
                own = any(c.get("page") == r["page"] and box_overlaps(box, r, flip) for r in regions)
                twin = any(c.get("page") == r["page"] and box_overlaps(box, r, flip) for r in twins)
                if own and not twin:
                    return True
    return False


def score_split(segments: list[dict], truth: list[dict]) -> list[dict]:
    """segments: list of {"class", "start", "end"}. One row per true segment.
    A blank page absorbed into its neighbour is accepted (the blank row already allows it), so a
    segment's range counts as exact if it covers the true range plus only adjacent blank pages."""
    blank = {p for t in truth if t["class"] is None for p in range(t["start"], t["end"] + 1)}
    rows = []
    for t in truth:
        got = [s for s in segments if s["start"] <= t["end"] and s["end"] >= t["start"]]
        want = set(range(t["start"], t["end"] + 1))
        exact = [s for s in got if want <= (have := set(range(s["start"], s["end"] + 1))) and have - want <= blank]
        absorbed = sorted({p for s in exact if s.get("class") == t["class"]
                           for p in range(s["start"], s["end"] + 1)} - want)
        if t["class"] is None:
            labelled = [s for s in got if s.get("class")]
            ok = not any(s["start"] == t["start"] and s["end"] == t["end"] and s.get("class") for s in got)
            rows.append({"pages": f"{t['start']}-{t['end']}", "expected": "blank (unlabelled/absorbed)",
                         "got": [f"{s.get('class')}:{s['start']}-{s['end']}" for s in got], "pass": ok,
                         "note": t.get("note", "") if ok else f"labelled as {labelled[0].get('class')}"})
            continue
        ok = any(s.get("class") == t["class"] for s in exact)
        note = t.get("note", "")
        if ok and absorbed:
            note = "; ".join(filter(None, [note, f"absorbed blank page {', '.join(map(str, absorbed))}"]))
        rows.append({"pages": f"{t['start']}-{t['end']}", "expected": t["class"],
                     "got": [f"{s.get('class')}:{s['start']}-{s['end']}" for s in got], "pass": ok,
                     "note": note})
    return rows
