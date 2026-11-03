#!/usr/bin/env python3
"""Build RESULTS.md and reports/ from the runs quoted in the write-up.

    python make_results.py

Reads results/<run>/report.md for each run in RUNS (results/ itself is not committed: raw responses
carry account IDs). Copies each report to reports/<run>.md and writes RESULTS.md from their check
rows and test 6 summaries. Nothing is typed in by hand except the run list and the prose.
"""

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RES, OUT = ROOT / "results", ROOT / "reports"

# (key, system, run folder, what it covers)
RUNS = [
    ("ndi_t125", "NDI", "20261028-145402-rescored", "tests 1, 2, 5 (low mode)"),
    ("ndi_t47", "NDI", "20261028-152125-rescored", "tests 4, 7 (low mode)"),
    ("ndi_t6", "NDI", "20261029-214329-rescored", "test 6, 100 invoices (low mode)"),
    ("ndi_high", "NDI", "20261030-103747", "tests 2, 4, 7 (high mode)"),
    ("claude", "Claude Opus 5.5", "frontier-20261029-220953", "tests 1, 2, 5, 7 table, 6"),
    ("docai", "Document AI", "docai-20261031-202256-rescored", "tests 1, 2, 4, 5, 6, 7"),
]
TWINS = ["line_item_amount", "subtotal", "total", "amount_paid"]


def parse(text):
    """Check rows as dicts, failed jobs by file, and the test 6 JSON summary."""
    rows, failed, test, file = [], {}, None, None
    for line in text.splitlines():
        if m := re.match(r"## (?:Test )?(\d)[.:]\s*(\S+\.\w+)?", line):
            test, file = int(m[1]), m[2]
        elif m := re.match(r"### (\S+\.\w+)", line):
            file = m[1]
        elif line.startswith("**Job failed:**"):
            failed[file] = line
        elif line.startswith("| ") and not line.startswith("| field") and test:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 6 and cells[4] in ("PASS", "FAIL"):
                rows.append({"test": test, "file": file, "field": cells[0], "check": cells[1],
                             "result": cells[4]})
    batch = None
    for blk in re.findall(r"```json\n(.*?)\n```", text, re.S):
        j = json.loads(blk)
        if j.get("docs") == 100:
            batch = j
    return rows, failed, batch


def score(rows, keep):
    hit = [r for r in rows if keep(r)]
    return (sum(r["result"] == "PASS" for r in hit), len(hit)) if hit else None


def cell(s, note=""):
    if s is None:
        return note or "—"
    return f"{s[0]}/{s[1]}" + (f" ({note})" if note else "")


def main():
    OUT.mkdir(exist_ok=True)
    R = {}
    for key, _, run, _ in RUNS:
        text = (RES / run / "report.md").read_text()
        shutil.copyfile(RES / run / "report.md", OUT / f"{run}.md")
        R[key] = parse(text)

    def s(key, keep):
        return score(R[key][0], keep)

    def link(key, label):
        run = next(r for k, _, r, _ in RUNS if k == key)
        return f"[{label}](reports/{run}.md)"

    t1 = lambda r: r["test"] == 1  # noqa: E731
    t2v = lambda r: r["test"] == 2 and r["check"] == "correct value"  # noqa: E731
    t2c = lambda r: r["test"] == 2 and r["check"].startswith("citation on the right instance") \
        and r["field"] in TWINS  # noqa: E731
    t2g = lambda r: r["test"] == 2 and r["field"].startswith("ground")  # noqa: E731
    t4 = lambda r: r["test"] == 4  # noqa: E731
    t5 = lambda r: r["test"] == 5  # noqa: E731
    t7t = lambda r: r["test"] == 7 and r["file"] == "multipage_table.pdf"  # noqa: E731
    t7x = lambda r: r["test"] == 7 and r["file"] == "q3_revenue.xlsx"  # noqa: E731

    def acc(key):
        b = R[key][2]
        return b["doc_accuracy_all_fields"] if b else "—"

    def eml(key):
        f = R[key][1].get("quote_thread.eml", "")
        return "not accepted" if "not run on Document AI" in f else "rejected by the API" if f else "—"

    def xlsx(key):
        f = R[key][1].get("q3_revenue.xlsx", "")
        return "not accepted" if "not run on Document AI" in f else cell(s(key, t7x))

    high4 = s("ndi_high", t4)
    table = [
        ("1. Missing fields → `not_found`", cell(s("ndi_t125", t1)), cell(s("claude", t1)), cell(s("docai", t1))),
        ("2. Repeated values: correct value", cell(s("ndi_t125", t2v)), cell(s("claude", t2v)),
         cell(s("docai", t2v))),
        ("2. Citation tight on the right copy (4 twin values)", cell(s("ndi_t125", t2c)), "no locations",
         cell(s("docai", t2c))),
        ("2. `ground` finds every copy", cell(s("ndi_t125", t2g)), "no equivalent", "no equivalent"),
        ("4. Packet split (5 page ranges)", cell(s("ndi_t47", t4), f"high mode: {cell(high4)}"), "not run",
         cell(s("docai", t4))),
        ("5. Prompt injection", cell(s("ndi_t125", t5)), cell(s("claude", t5)), cell(s("docai", t5))),
        ("6. 100 invoices, every field right", acc("ndi_t6"), acc("claude"), acc("docai")),
        ("7. Two-page table", cell(s("ndi_t47", t7t), f"high mode: {cell(s('ndi_high', t7t))}"),
         cell(s("claude", t7t)), cell(s("docai", t7t))),
        ("7. Spreadsheet", xlsx("ndi_t47"), "not run", xlsx("docai")),
        ("7. Email with attachment", eml("ndi_t47"), "not run", eml("docai")),
    ]

    # Test 2 citations, field by field
    def cite_rows(key):
        out = {}
        for r in R[key][0]:
            if r["test"] == 2 and r["check"].startswith("citation on the right instance"):
                out[r["field"]] = r["result"]
        return out

    cites = {k: cite_rows(k) for k in ("ndi_t125", "ndi_high", "docai")}

    # Test 6
    nb, cb, db = R["ndi_t6"][2], R["claude"][2], R["docai"][2]
    nf, df = nb["fresh_parse"], db["fresh_parse"]
    batch = [
        ("Invoices with every field right", nb["doc_accuracy_all_fields"], cb["doc_accuracy_all_fields"],
         db["doc_accuracy_all_fields"]),
        ("p50 latency (s)", nf["end_to_end_p50_s"], cb["latency_p50_s"], df["end_to_end_p50_s"]),
        ("p95 latency (s)", nf["end_to_end_p95_s"], round(cb["latency_p95_s"], 2), df["end_to_end_p95_s"]),
        ("$ per invoice", nf["usd_per_doc"], cb["usd_per_doc"], df["usd_per_doc"]),
        ("Latency measured as", "server, queued → finished", "request wall clock", "request wall clock"),
        ("$ from", "API charge", "tokens × list price", "pages × list price"),
        ("Invoices in the latency and $ figures", f"{nf['docs']} fresh parses", cb["docs"], df["docs"]),
    ]
    fields = list(nb["field_accuracy"])
    for f in fields:
        batch.append((f"`{f}`", nb["field_accuracy"][f], cb["field_accuracy"][f], db["field_accuracy"][f]))

    md = ["# Results: NDI vs Claude Opus 5.5 vs Google Document AI", "",
          "Generated by `make_results.py` from the reports in [reports/](reports/); not hand-edited. "
          "Each report is a copy of a run's `report.md`. The raw API responses stay local, because they "
          "carry account IDs.", "",
          "The Nace team asked for this test and provided console credits. There was no payment or "
          "sponsorship, and Nace had no say in what was tested and did not see the results before "
          "publication. The Claude and Document AI runs were paid for separately.", "",
          "## Checks passed, by test", "",
          "Source reports for each cell are listed under [Runs](#runs).", "",
          "| Test | NDI | Claude Opus 5.5 | Document AI |", "|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in table]
    md += ["", "NDI ran in its default low parse mode; tests 2, 4 and 7 were re-run in high mode "
           f"({link('ndi_high', 'report')}). Claude was given the PDF tests only. Document AI ran zero-shot, "
           "with each test's schema sent per request, and its answers were converted to NDI's job shape so the "
           "same scorer could check them.", "",
           "## Test 2: citations on the repeated values", "",
           "4,200.00 is both the line amount and the subtotal; 4,546.50 is both the total and the amount paid. "
           "The scored check passes when the citation box sits on the value's own copy and is tight around it. "
           "`balance_due` (0.00, not repeated) is shown for reference.", "",
           "| Field | NDI, low | NDI, high | Document AI |", "|---|---|---|---|"]
    for f in TWINS + ["balance_due"]:
        md.append(f"| `{f}` | {cites['ndi_t125'].get(f, '—')} | {cites['ndi_high'].get(f, '—')} | "
                  f"{cites['docai'].get(f, '—')} |")
    md += ["", "NDI's citations for the four repeated values land on the right copy but cover the whole line. "
           "Document AI's boxes cover the number alone. Claude returns no locations.", "",
           "## Test 6: 100 invoices", "",
           f"| | {link('ndi_t6', 'NDI')} | {link('claude', 'Claude Opus 5.5')} | {link('docai', 'Document AI')} |",
           "|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in batch]
    md += ["", "The timing difference favours NDI: wall clock also counts the network round trip. NDI's other "
           f"{nb['docs'] - nf['docs']} invoices reused an earlier parse, which is faster and cheaper, so they are "
           "left out of its latency and $.", "",
           "## Runs", "", "| System | Covers | Report |", "|---|---|---|"]
    md += [f"| {sysname} | {what} | [{run}](reports/{run}.md) |" for _, sysname, run, what in RUNS]
    md += ["", "Runs ending in `-rescored` were scored again from their saved responses after a harness fix, with "
           "no API calls. Every harness change, and why, is in [CHANGES.md](CHANGES.md).", ""]
    (ROOT / "RESULTS.md").write_text("\n".join(md))
    print("wrote RESULTS.md and", len(RUNS), "reports")


if __name__ == "__main__":
    main()
