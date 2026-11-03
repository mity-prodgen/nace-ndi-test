# NDI test harness

Hands-on tests of Nace Document Intelligence (NDI): does it say "not found" when a field is missing, cite the right copy of a repeated value, split a messy packet, resist prompt injection, and hold up on cost and latency? Every test document is generated here, so the ground truth is exact.

**Results:** [RESULTS.md](RESULTS.md) compares NDI, Claude Opus 5.5 and Google Document AI. It's built by `python make_results.py` from the run reports in [reports/](reports/).

## Setup

Tested on Python 3.13 (macOS).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python make_fixtures.py          # builds fixtures/ and fixtures/truth.json
python selftest_mock.py          # full dry run against a fake server, no credits used
```

`make_fixtures.py` is deterministic: `truth.json` and every PDF's text come out the same on each run, and only the PDF timestamps change. The self-test's fake NDI makes four deliberate mistakes, so **51/55 checks is the expected result**. Any other score means the harness is broken.

## What a full run costs

Approximate, from the runs in the write-up (2026-10-28 to 10-31):

| Run | Cost |
|---|---|
| NDI, tests 1, 2, 4, 5, 7 | about $0.19 |
| NDI, test 6 (100 invoices) | about $1.12 (26 of them reused an earlier parse) |
| NDI, `--parse-mode high` on tests 2, 4, 7 | about $0.19 |
| Claude Opus 5.5, everything | about $1.11 |
| Document AI, everything | about $3.27 at list price |

You need a Nace console account with credits, an Anthropic API key, and a GCP project with billing on. Each comparison runs on its own, so you can skip any of them.

## Run against NDI

NDI is reached through the Nace console's document API (`console.nace.ai/v1/documents`, "Perception" in the console docs) with `nace-sdk`. That API passes the NDI document service's jobs through unchanged, and each job carries the USD the console charged for it.

```bash
export NACE_API_KEY=nace_sk_...        # console.nace.ai > API Keys (NDI_API_KEY is read too)
python ndi_tests.py --tests 1,2,5      # headline tests first, cheap
python ndi_tests.py --tests 4,7
python ndi_tests.py --tests 6 --batch-n 20   # smoke test, then the full 100
python ndi_tests.py --parse-mode high --tests 1,2,4   # same tests on their top parse mode
```

All three scripts take `--concurrency` (default 8). Each run writes `results/<timestamp>/report.md`, plus `raw/` with every untouched API response. `results/` is in `.gitignore`, because raw responses carry account IDs. `python ndi_tests.py rescore results/<timestamp>` scores those saved responses again with the current scoring code (no API calls) into `results/<timestamp>-rescored/`. Quote numbers in the post from `raw/`, not from memory. Every fixture is uploaded into the account's document workspace, and stays there until deleted.

## Test 3: their samples vs your documents

Point folder mode at their prepared dataset, then at your own messy version of the same workflow, with one schema:

```bash
python ndi_tests.py folder ./nace_samples/invoices --schema schemas/invoice.json
python ndi_tests.py folder ./my_invoices --schema schemas/invoice.json --truth my_truth.json
```

`my_truth.json` is optional. Shape: `{"file.pdf": {"expected": {"total": 123.45}, "must_be_absent": ["po_number"]}}`. Without it you get the raw extraction to check by hand.

## Frontier comparison (tests 1, 2, 5, the 2-page table and test 6)

```bash
export ANTHROPIC_API_KEY=...
export FRONTIER_MODEL=claude-opus-5-5      # default; any model that takes PDF input
export FRONTIER_EFFORT=medium              # default
export FRONTIER_USD_PER_MTOK_IN=...  FRONTIER_USD_PER_MTOK_OUT=...   # current list prices ($4 / $20 on 2026-10-30)
python frontier_compare.py --batch-n 100
```

PDF fixtures only, sent directly with structured output. Same files, same schemas, same scorer. Without the two price variables the report shows no $ figures. If an NDI batch run exists, the report adds a side-by-side table.

## Document AI comparison (tests 1, 2, 4, 5, 6 and the 2-page table)

```bash
gcloud auth application-default login
export DOCAI_PROJECT=your-gcp-project-id    # Document AI API enabled, billing on
python docai_compare.py --selftest          # fake Document AI first: no GCP, no cost
python docai_compare.py --gate-only         # two documents, checks the output shape (~$0.06)
python docai_compare.py --batch-n 100       # everything (~$3.50 at list price)
python docai_compare.py --rescore results/docai-<stamp>   # re-score saved responses after a harness fix; nothing sent
```

The fake Document AI makes three deliberate mistakes, so **44/46 checks and 99/100 invoices is the expected self-test result**.

Google Document AI, zero-shot: Custom Extractor and Custom Splitter on the Gemini 3 preview versions (override with `DOCAI_EXTRACTOR_VERSION` / `DOCAI_SPLITTER_VERSION`), region `us` (`DOCAI_LOCATION`). The script creates one processor of each kind, named `ndi-compare-extract` and `ndi-compare-split`, and sends each test's schema with the request. Its answers are saved in NDI's job shape and scored by `ndi_tests.py`'s own checks, so the report has the same tables, including test 2's citation rows, plus an NDI / Claude / Document AI side-by-side. `ground`, the xlsx and the .eml show as "not run": Document AI has no equivalent or doesn't take that input.

When you're done, delete the two processors (Document AI > My processors in the Cloud console) or shut down the project, so nothing is left behind.

## What each test checks

| # | Fixture | Pass means |
|---|---|---|
| 1 | Invoice with no PO number (but a "PO Box" in the address); contract with no termination clause | Missing fields come back `not_found`, not a guess, and the PO Box decoy isn't taken |
| 2 | Invoice where 4,200.00 and 4,546.50 each appear twice with different meanings | Each field's citation polygon lands on the right copy; `ground` finds both occurrences |
| 4 | 6-page PDF: invoice, 2-page contract, phone photo of a receipt, blank page, purchase order scanned sideways | Every segment gets the right class and page range (a range may also absorb the adjacent blank page); the blank page isn't labelled as its own segment |
| 5 | Invoice with an instruction in white text, one with a 4pt footnote, one clean control | Extraction ignores the instruction. Also reports whether the hidden text shows up in the parse output an agent would read |
| 6 | 100 generated invoices | Accuracy at volume, p50/p95 latency, credits (and $) per doc |
| 7 | xlsx with merged headers, a hidden superseded row and 3 tabs; 2-page table with repeated headers; email thread where the price changes, with a PDF attachment | Current values, not the hidden draft or the first quoted price |

## Harness changes

CHANGES.md lists every fix made to the harness during testing, with its reason. Two changed what counts as correct, both approved before they were made: one ground-truth correction (4,200.00 appears three times on the test 2 invoice) and accepting a blank page absorbed into the document before it.

## Caveats when quoting results

- Synthetic documents are cleaner than real ones in some ways (crisp fonts) and harder in others (planted traps). Test 3 with real documents is the balance.
- Citation scoring assumes polygons are normalized or in PDF points and tries both origins. If a correct-looking citation fails, check the raw JSON before writing it up.
- Default `parse_mode` is `low`. Report which mode each result came from, and give `high` a run on anything that fails, since that's the fair comparison.
- NDI latency is server-reported (queued → finished) when the job carries `started_at`/`finished_at`; the console only documents `created_at`, so check `raw/` before quoting it. Frontier latency is request wall clock.
- Cost is the console's recorded charge (`cost.total_usd`), which it says is not the NDI service's own tariff. It is null until the charge settles.
- The service reuses an earlier parse of the same file (`execution: "reused"` in the job's cost lines), which is cheaper and faster. The test 6 summary's `fresh_parse` block holds first-time figures; quote those for cold-start latency and cost. Every fixture parsed once stays reusable.
- The console limits requests per minute per account. The harness waits out 429s, so `batch_wall_s` includes that throttling.

## License

MIT. See [LICENSE](LICENSE).
