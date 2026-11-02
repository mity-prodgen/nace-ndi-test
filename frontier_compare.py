"""Same documents, same schemas, same scoring, sent to a frontier model reading the PDF directly.

    export ANTHROPIC_API_KEY=...
    export FRONTIER_MODEL=claude-opus-5-5          # or any model that accepts PDF input
    export FRONTIER_USD_PER_MTOK_IN=...            # list price, from the provider's pricing page
    export FRONTIER_USD_PER_MTOK_OUT=...
    python frontier_compare.py [--batch-n 100] [--concurrency 8]

PDF fixtures only (tests 1, 2, 5, the multi-page table, and the 100-invoice batch).
The xlsx and .eml files are skipped because the model can't read them natively and
converting them first would be testing my converter, not the model.
No citations are scored: the model returns values only, so grounding is NDI-only.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic

from scoring import score_values

ROOT = Path(__file__).parent
FIX = ROOT / "fixtures"
MODEL = os.environ.get("FRONTIER_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("FRONTIER_EFFORT", "medium")  # Opus 5.5's own default, stated so the report can say it
PRICE_IN = float(os.environ.get("FRONTIER_USD_PER_MTOK_IN", "0") or 0)
PRICE_OUT = float(os.environ.get("FRONTIER_USD_PER_MTOK_OUT", "0") or 0)

# Neutral instruction: the same thing NDI is asked to do, nothing tuned to the tests.
SYSTEM = ("Extract the requested fields from the attached document. "
          "Use null for any field the document does not state.")


def nullable(schema: dict) -> dict:
    s = copy.deepcopy(schema)
    for k, v in s.get("properties", {}).items():
        t = v.get("type")
        if isinstance(t, str):
            v["type"] = [t, "null"]
    s["required"] = list(s.get("properties", {}))
    s["additionalProperties"] = False  # required by structured outputs
    return s


def call(client: anthropic.Anthropic, path: Path, schema: dict) -> dict:
    pdf = base64.standard_b64encode(path.read_bytes()).decode()
    t0 = time.perf_counter()
    # Structured output instead of a forced tool call: Opus 5.5 rejects forced tool_choice with a 400.
    # A refusal falls back server-side; served_by records which model actually answered.
    msg = client.beta.messages.create(
        model=MODEL, max_tokens=16000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": nullable(schema)}},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}},
            {"type": "text", "text": "Extract the fields."},
        ]}],
    )
    wall = time.perf_counter() - t0
    if msg.stop_reason != "end_turn":
        raise RuntimeError(f"stop_reason={msg.stop_reason}")
    data = json.loads(next(b.text for b in msg.content if b.type == "text"))
    u = msg.usage
    fell_back = any(it.type == "fallback_message" for it in (getattr(u, "iterations", None) or []))
    usd = (u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT) / 1e6 if PRICE_IN else None
    return {"data": data, "wall_s": round(wall, 3), "in_tok": u.input_tokens, "out_tok": u.output_tokens, "usd": usd,
            "served_by": msg.model, "fell_back": fell_back}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")
    T = json.loads((FIX / "truth.json").read_text())

    jobs = []  # (section, name, path, schema, case)
    for name, case in T["t1_absent"].items():
        jobs.append(("1", name, FIX / "t1_absent" / name, case["schema"], case))
    for name, case in T["t2_grounding"].items():
        jobs.append(("2", name, FIX / "t2_grounding" / name, case["schema"], case))
    for name, case in T["t5_injection"].items():
        jobs.append(("5", name, FIX / "t5_injection" / name, case["schema"], case))
    mp = T["t7_odd"]["multipage_table.pdf"]
    jobs.append(("7", "multipage_table.pdf", FIX / "t7_odd" / "multipage_table.pdf", mp["schema"], mp))
    for name in sorted(T["t6_batch"]["docs"])[: a.batch_n]:
        jobs.append(("6", name, FIX / "t6_batch" / name, T["t6_batch"]["schema"], T["t6_batch"]["docs"][name]))

    client = anthropic.Anthropic()
    out = []
    with ThreadPoolExecutor(a.concurrency) as ex:
        futs = {ex.submit(call, client, p, s): (sec, n, c) for sec, n, p, s, c in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            sec, name, case = futs[f]
            try:
                r = f.result()
                rows = score_values(r["data"], {}, case)
                out.append({"section": sec, "doc": name, **r, "rows": rows})
            except Exception as e:  # noqa: BLE001 - record and keep going
                out.append({"section": sec, "doc": name, "error": repr(e)})
            print(f"  {i}/{len(jobs)}", end="\r")
    print()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = ROOT / "results" / f"frontier-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps(out, indent=2, default=str))

    fell_back = sorted(x["doc"] for x in out if x.get("fell_back"))
    lines = [f"# Frontier comparison: {MODEL}", "",
             f"Effort `{EFFORT}`, structured JSON output, PDF sent as a base64 document. "
             f"Refusals fall back server-side (`fallbacks: \"default\"`); "
             f"answered by a fallback model: {', '.join(fell_back) if fell_back else 'none'}.", ""]
    for sec in ["1", "2", "5", "7"]:
        for r in sorted((x for x in out if x["section"] == sec), key=lambda x: x["doc"]):
            lines.append(f"## Test {sec}: {r['doc']}")
            if "error" in r:
                lines += ["", f"**Error:** `{r['error']}`", ""]; continue
            lines += ["", "| field | check | expected | got | result | note |", "|---|---|---|---|---|---|"]
            for row in r["rows"]:
                lines.append(f"| {row['field']} | {row['check']} | {row.get('expected')} | {row.get('got')} | "
                             f"{'PASS' if row['pass'] else 'FAIL'} | {row.get('note', '')} |")
            lines.append("")

    batch = [x for x in out if x["section"] == "6" and "error" not in x]
    if batch:
        walls = sorted(x["wall_s"] for x in batch)
        q = lambda p: walls[min(len(walls) - 1, int(round(p * (len(walls) - 1))))]
        fields: dict[str, list[bool]] = {}
        for x in batch:
            for row in x["rows"]:
                fields.setdefault(row["field"], []).append(row["pass"])
        usd = [x["usd"] for x in batch if x["usd"] is not None]
        summary = {
            "model": MODEL, "docs": len(batch),
            "errors": sum(1 for x in out if x["section"] == "6" and "error" in x),
            "latency_p50_s": q(.5), "latency_p95_s": q(.95),
            "avg_input_tokens": round(statistics.mean(x["in_tok"] for x in batch)),
            "avg_output_tokens": round(statistics.mean(x["out_tok"] for x in batch)),
            "usd_per_doc": round(statistics.mean(usd), 5) if usd else "set FRONTIER_USD_PER_MTOK_IN/OUT",
            "field_accuracy": {f: f"{sum(v)}/{len(v)}" for f, v in fields.items()},
            "doc_accuracy_all_fields": f"{sum(1 for x in batch if all(r['pass'] for r in x['rows']))}/{len(batch)}",
        }
        lines += ["## Test 6: batch", "", "```json", json.dumps(summary, indent=2), "```", ""]

        # side by side with the latest NDI batch run, if there is one
        ndi_runs = sorted(p for p in (ROOT / "results").glob("2*/raw/t6_summary.json"))
        if ndi_runs:
            n = json.loads(ndi_runs[-1].read_text())
            # latency and cost from first-time parses only: a reused parse is cheaper and faster,
            # and the frontier model has no equivalent, so mixing them would flatter NDI
            f = n.get("fresh_parse") or {}
            lat_cost = {k: f.get(k, n.get(k)) for k in ("end_to_end_p50_s", "end_to_end_p95_s", "usd_per_doc")}
            lines += ["## NDI vs frontier, same invoices", "",
                      f"NDI figures from `{ndi_runs[-1].parent.parent.relative_to(ROOT)}`. NDI latency and $ are from "
                      f"the {f.get('docs', n['succeeded'])} invoices whose parse was not reused from an earlier run.", "",
                      "| | NDI | " + MODEL + " |", "|---|---|---|",
                      f"| docs | {n['succeeded']}/{n['docs']} | {len(batch)}/{len(batch)} |",
                      f"| all fields correct | {n['doc_accuracy_all_fields']} | {summary['doc_accuracy_all_fields']} |",
                      f"| p50 latency (s) | {lat_cost['end_to_end_p50_s']} | {summary['latency_p50_s']} |",
                      f"| p95 latency (s) | {lat_cost['end_to_end_p95_s']} | {summary['latency_p95_s']} |",
                      f"| $ per doc | {lat_cost['usd_per_doc'] or 'not settled'} | {summary['usd_per_doc']} |", "",
                      "NDI latency is server end-to-end (queued to finished); frontier latency is request wall clock.", ""]
    (d / "report.md").write_text("\n".join(lines))
    print(f"Report: {d / 'report.md'}")


if __name__ == "__main__":
    main()
