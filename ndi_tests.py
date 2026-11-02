"""Run the NDI test suite against the synthetic fixtures (or your own folder).

NDI is reached through the Nace console's document API (console.nace.ai/v1/documents,
"Perception"), which passes the NDI document service's jobs through unchanged.

    export NACE_API_KEY=nace_sk_...          # NDI_API_KEY is read too
    python ndi_tests.py                      # all tests: 1 2 4 5 6 7
    python ndi_tests.py --tests 1,2,5        # just the headline tests
    python ndi_tests.py --batch-n 20         # smaller cost/latency run first
    python ndi_tests.py folder ./my_invoices --schema schemas/invoice.json [--truth my_truth.json]

Every raw API response is saved under results/<run>/raw/ so any claim in the
post can be traced back to an actual response.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from nace_sdk import NaceClient, NaceError, RetryPolicy

from scoring import citation_on_right_copy, score_citation, score_split, score_values

ROOT = Path(__file__).parent
FIX = ROOT / "fixtures"


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
class Run:
    def __init__(self, parse_mode: str, name: str | None = None, note: str = "",
                 label: str = "NDI test run", api: str | None = None):
        self.dir = ROOT / "results" / (name or datetime.now().strftime("%Y%m%d-%H%M%S"))
        (self.dir / "raw").mkdir(parents=True, exist_ok=True)
        self.parse_mode = parse_mode
        self.note = note
        self.label = label  # report title; another product's run scored by these tests sets its own
        self.api = api or f"console.nace.ai document API via nace-sdk {version('nace-sdk')}"
        self.sections: dict[str, Any] = {}
        self.jobs: list[dict] = []  # timing/credits for every job

    def save_raw(self, name: str, obj: Any):
        p = self.dir / "raw" / f"{name}.json"
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))


# The SDK's default retry budget is 30 s, shorter than a per-minute rate limit's retry-after, so a
# 100-document batch failed on 429s. Retries reuse the call's idempotency key, so none is billed twice.
RETRY = RetryPolicy(max_retries=8, timeout=300.0)


def api_key() -> str | None:
    return os.environ.get("NACE_API_KEY") or os.environ.get("NDI_API_KEY")


def client() -> NaceClient:
    if not api_key():
        sys.exit("Set NACE_API_KEY first (a nace_sk_ key from the API Keys page on console.nace.ai).")
    return NaceClient(api_key=api_key(), retry=RETRY)


REPLAY: Path | None = None  # rescore mode: a past run whose raw/ is read instead of calling the API


def run_job(c: NaceClient, run: Run, label: str, op: str, path: Path, **kw) -> dict:
    """Upload, start one operation, wait for it, record timing + credits + raw output."""
    if REPLAY:
        return replay_job(run, label, op, path)
    t0 = time.perf_counter()
    try:
        # new_version: a regenerated fixture under the same name would otherwise be a path_conflict
        source = c.documents.upload(str(path), on_conflict="new_version").as_source()
        started = getattr(c.documents, op)(source, **kw)
        job = c.jobs.wait(started.job_id, timeout=900, raise_on_failure=False)
    except NaceError as e:
        rec = {"label": label, "op": op, "file": path.name, "ok": False,
               "error": f"{type(e).__name__}: {getattr(e, 'code', '') or ''} {e}"}
        run.jobs.append(rec)
        run.save_raw(label, rec)
        return rec
    wall = time.perf_counter() - t0
    raw = job.model_dump_json(indent=2, by_alias=True)
    run.save_raw(label, raw)
    rec = job_record(label, op, path, json.loads(raw), round(wall, 3))
    run.jobs.append({k: v for k, v in rec.items() if k != "result"})
    return rec


def replay_job(run: Run, label: str, op: str, path: Path) -> dict:
    """The record run_job made for `label` in the REPLAY run, rebuilt from its raw JSON."""
    body = json.loads((REPLAY / "raw" / f"{label}.json").read_text())
    if "job_id" not in body:  # the job never started; raw holds the error record itself
        run.jobs.append(body)
        return body
    walls = {j["label"]: j.get("client_wall_s") for j in json.loads((REPLAY / "jobs.json").read_text())}
    rec = job_record(label, op, path, body, walls.get(label))
    run.jobs.append({k: v for k, v in rec.items() if k != "result"})
    return rec


def job_record(label: str, op: str, path: Path, body: dict, wall: float | None) -> dict:
    """One job's timing, cost and result, from the job JSON exactly as saved in raw/."""
    # The job is the document service's body passed through; timestamps beyond created_at are extras.
    def ts(key):
        v = body.get(key)
        return datetime.fromisoformat(v) if isinstance(v, str) else None

    def secs(a, b):
        return (b - a).total_seconds() if a and b else None

    cost = body.get("cost") or {}
    return {
        "label": label, "op": op, "file": path.name, "job_id": body["job_id"], "ok": body["status"] == "succeeded",
        "status": body["status"], "error": body.get("error"),
        "units": body.get("units"), "credits": body.get("credits"),
        "usd": float(cost["total_usd"]) if cost.get("total_usd") is not None else None,
        # the service reuses an earlier parse of the same file: cheaper and faster than a first run
        "parse_reused": any(ln.get("execution") == "reused" for ln in cost.get("lines") or []),
        "server_run_s": secs(ts("started_at"), ts("finished_at")),
        "server_total_s": secs(ts("created_at"), ts("finished_at")),
        "client_wall_s": wall,
        "result": body.get("result"),
    }


def parse_markdown(c: NaceClient, run: Run, label: str, rec: dict) -> str:
    """Parse markdown, inline or, when the service defers it, from the job file `content_url` names."""
    doc = (rec["result"] or {}).get("document") or {}
    if doc.get("markdown"):
        return doc["markdown"]
    if doc.get("content_url"):
        if REPLAY:
            return (REPLAY / "raw" / f"{label}.md").read_text()
        dest = run.dir / "raw" / f"{label}.md"
        c.jobs.download(rec["job_id"], doc["content_url"], dest)
        return dest.read_text()
    return (rec["result"] or {}).get("markdown") or ""


def field_key(path: str) -> str:
    return path.lstrip("$./").replace("/", ".")


def unpack_extract(result: dict) -> tuple[dict, dict, dict]:
    data = result.get("data") or {}
    status, cites = {}, {}
    for f in result.get("fields", []):
        k = field_key(f["path"])
        status[k] = f.get("status")
        cites[k] = [c["location"] for c in f.get("citations", []) if c.get("location")]
        if k not in data:
            data[k] = f.get("value")
    return data, status, cites


def extract(c, run, label, path, schema) -> dict:
    return run_job(c, run, label, "extract", path, schema=schema, parse_mode=run.parse_mode)


def truth() -> dict:
    p = FIX / "truth.json"
    if not p.exists():
        sys.exit("No fixtures yet. Run: python make_fixtures.py")
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test1(c, run, T):
    out = []
    for name, case in T["t1_absent"].items():
        rec = extract(c, run, f"t1_{name}", FIX / "t1_absent" / name, case["schema"])
        if not rec["ok"]:
            out.append({"doc": name, "error": rec["error"]}); continue
        data, status, _ = unpack_extract(rec["result"])
        out.append({"doc": name, "rows": score_values(data, status, case)})
    run.sections["1. Fields that aren't in the document"] = out


def test2(c, run, T):
    out = []
    for name, case in T["t2_grounding"].items():
        path = FIX / "t2_grounding" / name
        rec = extract(c, run, f"t2_extract_{name}", path, case["schema"])
        if not rec["ok"]:
            out.append({"doc": name, "error": rec["error"]}); continue
        data, status, cites = unpack_extract(rec["result"])
        rows = score_values(data, status, case)
        want = case["expected"]
        for field, regions in case["regions"].items():
            locs = [l for l in cites.get(field, []) if l.get("kind") == "page_region"]
            s = score_citation(locs, regions)
            rows.append({"field": field, "check": "citation on the right instance", **s,
                         "got": f"{len(cites.get(field, []))} citation(s)"})
            # the other fields carrying the same value are this field's twins
            twins = [r for g, rs in case["regions"].items() if g != field and want.get(g) == want.get(field) for r in rs]
            on_copy = citation_on_right_copy(locs, regions, twins)
            rows.append({"field": field, "check": "citation on the right copy (info)",
                         "expected": "own copy, not the twin", "got": "yes" if on_copy else "no", "pass": None,
                         "note": "ignores box tightness; the row above is the scored check"})
        # ground: the same text appears twice; does it find both?
        targets = [{"id": f"g{i}", "text": t} for i, t in enumerate(case["ground_targets"])]
        g = run_job(c, run, f"t2_ground_{name}", "ground", path, targets=targets,
                    options={"max_matches": 10})
        if g["ok"]:
            by_id = {tr["id"]: tr for tr in g["result"]["targets"]}
            for i, (text, want) in enumerate(case["ground_targets"].items()):
                n = len(by_id.get(f"g{i}", {}).get("matches", []))
                rows.append({"field": f'ground "{text}"', "check": f"finds all {want} occurrences",
                             "expected": want, "got": n, "pass": n == want})
        else:  # say so rather than drop the rows; not scored, since nothing was answered
            rows.append({"field": "ground", "check": "finds every occurrence", "expected": "(not run)",
                         "got": "—", "pass": None, "note": g["error"]})
        out.append({"doc": name, "rows": rows})
    run.sections["2. Grounding when a value appears twice"] = out


def test4(c, run, T):
    out = []
    for name, case in T["t4_packet"].items():
        rec = run_job(c, run, f"t4_split_{name}", "split", FIX / "t4_packet" / name,
                      classes=case["classes"], parse_mode=run.parse_mode)
        if not rec["ok"]:
            out.append({"doc": name, "error": rec["error"]}); continue
        segs = []
        for s in rec["result"]["segments"]:
            cls = (s.get("class") or {}).get("id") if s.get("status") == "matched" else None
            segs.append({"class": cls, "start": s.get("start_page"), "end": s.get("end_page"),
                         "conf": (s.get("classification_confidence") or {}).get("band")})
        out.append({"doc": name, "segments_returned": segs, "rows": score_split(segs, case["segments"])})
    run.sections["4. Splitting a mixed packet"] = out


def test5(c, run, T):
    out = []
    for name, case in T["t5_injection"].items():
        path = FIX / "t5_injection" / name
        rec = extract(c, run, f"t5_extract_{name}", path, case["schema"])
        if not rec["ok"]:
            out.append({"doc": name, "error": rec["error"]}); continue
        data, status, _ = unpack_extract(rec["result"])
        rows = score_values(data, status, case)
        p = run_job(c, run, f"t5_parse_{name}", "parse", path, parse_mode=run.parse_mode,
                    output={"formats": ["markdown"]})
        if p["ok"]:
            md = parse_markdown(c, run, f"t5_parse_{name}", p)
            passed_through = case["injection_text_marker"] in md.lower()
            rows.append({"field": "parse markdown", "check": "hidden text passed to agent?",
                         "expected": "(informational)", "got": "yes" if passed_through else "no",
                         "pass": None, "note": "white/tiny text present in output the agent reads"
                         if passed_through and case["has_injection"] else ""})
        out.append({"doc": name, "rows": rows})
    run.sections["5. Prompt injection hidden in the PDF"] = out


def test6(c_unused, run, T, n: int, concurrency: int):
    case = T["t6_batch"]
    names = sorted(case["docs"])[:n]
    results: dict[str, dict] = {}

    def work(name):
        with nullcontext() if REPLAY else NaceClient(api_key=api_key(), retry=RETRY) as c:
            return name, extract(c, run, f"t6_{name}", FIX / "t6_batch" / name, case["schema"])

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as ex:
        for fut in as_completed([ex.submit(work, nm) for nm in names]):
            name, rec = fut.result()
            results[name] = rec
            print(f"  t6 {len(results)}/{len(names)}", end="\r")
    elapsed = time.perf_counter() - t0
    print()

    ok = [r for r in results.values() if r["ok"]]
    field_hits: dict[str, list[bool]] = {}
    for name, r in results.items():
        if not r["ok"]:
            continue
        data, status, _ = unpack_extract(r["result"])
        for row in score_values(data, status, case["docs"][name]):
            field_hits.setdefault(row["field"], []).append(row["pass"])

    def pct(xs, q):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 2)

    run_s = [r.get("server_run_s") for r in ok]
    tot_s = [r.get("server_total_s") for r in ok]
    credits = [r.get("credits") for r in ok if r.get("credits") is not None]
    usd = [r.get("usd") for r in ok if r.get("usd") is not None]  # the console's recorded charge per job
    fresh = [r for r in ok if not r.get("parse_reused")]  # first-time documents, the fair cold-start figures
    fresh_usd = [r["usd"] for r in fresh if r.get("usd") is not None]
    summary = {
        "docs": len(names), "succeeded": len(ok), "failed": len(names) - len(ok),
        "concurrency": concurrency, "batch_wall_s": round(elapsed, 1),
        "server_run_p50_s": pct(run_s, .5), "server_run_p95_s": pct(run_s, .95),
        "end_to_end_p50_s": pct(tot_s, .5), "end_to_end_p95_s": pct(tot_s, .95),
        "credits_total": sum(credits) if credits else None,
        "credits_per_doc": round(statistics.mean(credits), 2) if credits else None,
        "usd_total": round(sum(usd), 5) if usd else None,
        "usd_per_doc": round(statistics.mean(usd), 5) if usd else None,
        "usd_settled_jobs": f"{len(usd)}/{len(ok)}",
        "parse_reused_jobs": f"{len(ok) - len(fresh)}/{len(ok)}",
        "fresh_parse": {
            "docs": len(fresh),
            "server_run_p50_s": pct([r.get("server_run_s") for r in fresh], .5),
            "server_run_p95_s": pct([r.get("server_run_s") for r in fresh], .95),
            "end_to_end_p50_s": pct([r.get("server_total_s") for r in fresh], .5),
            "end_to_end_p95_s": pct([r.get("server_total_s") for r in fresh], .95),
            "usd_per_doc": round(statistics.mean(fresh_usd), 5) if fresh_usd else None,
        },
        "field_accuracy": {f: f"{sum(v)}/{len(v)}" for f, v in field_hits.items()},
        "doc_accuracy_all_fields": f"{sum(1 for nm, r in results.items() if r['ok'] and all(row['pass'] for row in score_values(*unpack_extract(r['result'])[:2], case['docs'][nm])))}/{len(ok)}",
    }
    run.save_raw("t6_summary", summary)
    run.sections["6. Cost and latency at volume"] = [{"doc": f"{len(names)} invoices", "summary": summary}]


def test7(c, run, T):
    out = []
    for name, case in T["t7_odd"].items():
        rec = extract(c, run, f"t7_{name}", FIX / "t7_odd" / name, case["schema"])
        if not rec["ok"]:
            out.append({"doc": name, "error": rec["error"]}); continue
        data, status, _ = unpack_extract(rec["result"])
        out.append({"doc": name, "rows": score_values(data, status, case)})
    run.sections["7. Odd file types"] = out


def folder_mode(c, run, folder: Path, schema_path: Path, truth_path: Path | None):
    """Test 3: run their sample data and your own messy documents through one schema."""
    schema = json.loads(schema_path.read_text())
    tr = json.loads(truth_path.read_text()) if truth_path else {}
    out = []
    for path in sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")):
        rec = extract(c, run, f"folder_{path.name}", path, schema)
        if not rec["ok"]:
            out.append({"doc": path.name, "error": rec["error"]}); continue
        data, status, _ = unpack_extract(rec["result"])
        if path.name in tr:
            out.append({"doc": path.name, "rows": score_values(data, status, tr[path.name])})
        else:
            out.append({"doc": path.name, "extracted": data, "status": status})
    run.sections[f"3. Folder run: {folder.name}"] = out


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def fmt(v):
    if v is None:
        return "—"
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) < 60 else s[:57] + "..."


def write_report(run: Run):
    lines = [f"# {run.label} {run.dir.name}", "", f"parse_mode: `{run.parse_mode}`  ", f"API: {run.api}", ""]
    if run.note:
        lines += [f"**{run.note}**", ""]
    total_pass = total = 0
    for title, items in run.sections.items():
        lines += [f"## {title}", ""]
        for it in items:
            lines.append(f"### {it['doc']}")
            if "error" in it:
                lines += ["", f"**Job failed:** `{it['error']}`", ""]; continue
            if "summary" in it:
                lines += ["", "```json", json.dumps(it["summary"], indent=2), "```", ""]; continue
            if "segments_returned" in it:
                lines += ["", "Segments returned: " + ", ".join(
                    f"{s['class']}:{s['start']}-{s['end']}" for s in it["segments_returned"]), ""]
            if "rows" in it:
                lines += ["", "| field / pages | check | expected | got | result | note |", "|---|---|---|---|---|---|"]
                for r in it["rows"]:
                    res = "PASS" if r.get("pass") is True else ("FAIL" if r.get("pass") is False else "info")
                    if r.get("pass") is not None:
                        total += 1; total_pass += r["pass"] is True
                    lines.append(f"| {r.get('field', r.get('pages'))} | {r.get('check', 'split')} | "
                                 f"{fmt(r.get('expected', r.get('want')))} | {fmt(r.get('got'))} | {res} | "
                                 f"{fmt(r.get('note', '') or (r.get('status') or ''))} |")
                lines.append("")
            if "extracted" in it:
                lines += ["", "```json", json.dumps({"data": it["extracted"], "status": it["status"]}, indent=2, default=str), "```", ""]
    credits = [j.get("credits") for j in run.jobs if j.get("credits") is not None]
    usd = [j.get("usd") for j in run.jobs if j.get("usd") is not None]
    lines += ["## Totals", "", f"- Checks passed: **{total_pass}/{total}**",
              f"- Jobs: {len(run.jobs)} ({sum(1 for j in run.jobs if not j.get('ok'))} failed)",
              f"- Credits used: {sum(credits) if credits else 'not reported'}",
              f"- USD charged: {round(sum(usd), 5) if usd else 'not settled yet'} ({len(usd)}/{len(run.jobs)} jobs settled)", ""]
    (run.dir / "report.md").write_text("\n".join(lines))
    (run.dir / "jobs.json").write_text(json.dumps(run.jobs, indent=2, default=str))
    (run.dir / "sections.json").write_text(json.dumps(run.sections, indent=2, default=str))
    print(f"\n{total_pass}/{total} checks passed. Report: {run.dir / 'report.md'}")


def rescore(src: Path):
    """Score a past run's raw/ again with the current scoring code. No API calls, no credits.

    Writes results/<run>-rescored/, so the original report stays as it was.
    """
    report = (src / "report.md").read_text()
    mode = re.search(r"parse_mode: `(\w+)`", report)
    title = re.match(r"# (.+) \S+\n", report)
    api = re.search(r"^API: (.+)$", report, re.M)
    run = Run(mode.group(1) if mode else "low", name=f"{src.name}-rescored",
              note=f"Re-scored on {datetime.now():%Y-%m-%d} from {src.name}/raw with no API calls.",
              label=title.group(1) if title else "NDI test run", api=api.group(1) if api else None)
    t6 = src / "raw" / "t6_summary.json"
    old = json.loads(t6.read_text()) if t6.exists() else {}
    replay_tests(src, run, old.get("concurrency"), old.get("batch_wall_s"))
    write_report(run)


def replay_tests(src: Path, run: Run, concurrency: int | None, batch_wall_s: float | None):
    """Score every test that has job bodies in src/raw, without calling any API.

    Used by rescore, and by docai_compare.py, which saves another product's answers in the same shape."""
    global REPLAY
    REPLAY = src.resolve()
    T = truth()
    have = lambda n: any((src / "raw").glob(f"t{n}_*.json"))
    for num, fn in [("1", test1), ("2", test2), ("4", test4), ("5", test5), ("7", test7)]:
        if have(num):
            print(f"test {num} (replay) ..."); fn(None, run, T)
    if have(6):
        n = len(list((src / "raw").glob("t6_inv_*.json")))
        print("test 6 (replay) ..."); test6(None, run, T, n, concurrency)
        summary = run.sections["6. Cost and latency at volume"][0]["summary"]
        summary["batch_wall_s"] = batch_wall_s  # a replay takes no time; keep the real run's
        run.save_raw("t6_summary", summary)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="suite", choices=["suite", "folder", "rescore"])
    ap.add_argument("folder", nargs="?", help="folder mode: documents; rescore mode: a results/<run> dir")
    ap.add_argument("--tests", default="1,2,4,5,6,7")
    ap.add_argument("--batch-n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--parse-mode", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--schema", type=Path)
    ap.add_argument("--truth", type=Path)
    a = ap.parse_args()

    if a.mode == "rescore":
        rescore(Path(a.folder or sys.exit("rescore mode needs a results/<run> dir")))
        return
    run = Run(a.parse_mode)
    with client() as c:
        if a.mode == "folder":
            if not a.folder or not a.schema:
                sys.exit("folder mode needs a folder and --schema")
            folder_mode(c, run, Path(a.folder), a.schema, a.truth)
        else:
            T = truth()
            todo = {t.strip() for t in a.tests.split(",")}
            for num, fn in [("1", test1), ("2", test2), ("4", test4), ("5", test5), ("7", test7)]:
                if num in todo:
                    print(f"test {num} ..."); fn(c, run, T)
            if "6" in todo:
                print("test 6 ..."); test6(c, run, T, a.batch_n, a.concurrency)
    write_report(run)


if __name__ == "__main__":
    main()
