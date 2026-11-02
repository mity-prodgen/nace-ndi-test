# Harness changes

Every change made to the harness after it left the mock, and why. Changes 9–11 also cover runs that were redone or split; see each one. One pass condition was changed, with explicit approval: test 4's segment-range check now accepts an absorbed blank page, which the blank-page row already allowed (change 8). Every other pass condition is unchanged. `fixtures/truth.json` has one correction, a miscounted expected value checked against the PDF itself (change 7). No fixture document was changed.

## 1. Moved from `ndi-sdk` to the console document API (`nace-sdk` 0.4.0)

**Why:** the harness was written against `ndi-sdk`, which calls `ndi-api.nace.ai` with an `ndi_sk_` key in `X-API-Key`. The only key the Nace console issues is a `nace_sk_` key for `console.nace.ai`, sent as `Authorization: Bearer`. With it, every `ndi-api.nace.ai` call returned `401 Missing or invalid X-API-Key` (run `results/20261028-105918`, which has no results; don't quote it).

The console's document API (`/v1/documents/{parse,split,extract,ground}`, called "Perception" in its docs) fronts the same NDI document service:
- The `nace-sdk` docs for its job type say it is "the document service's job passed through unchanged".
- The console's OpenAPI spec describes a job's `cost` as the "Drex customer charge in USD, not the NDI service tariff".

So results now come from NDI *as served through console.nace.ai*. Every report says this in its header.

What changed in `ndi_tests.py`, all in the client layer:

| Before (`ndi-sdk`) | After (`nace-sdk`) | Note |
|---|---|---|
| `NDI_API_KEY`, `ndi_sk_` | `NACE_API_KEY`, falls back to `NDI_API_KEY` | |
| `documents.create_upload(path)` | `documents.upload(path, on_conflict="new_version").as_source()` | Fixtures now persist in the account's document workspace. `new_version` stops a regenerated fixture with the same name from failing as `path_conflict`. |
| `extract(json_schema=…)` | `extract(schema=…)` | Same JSON schema, same `parse_mode`. Citations default to on, delivery to `complete`. |
| `GroundTarget`/`GroundOptions(max_matches=10)` | plain dicts, `options={"max_matches": 10}` | Same values. |
| `SplitCategory(**k)` | the class dicts from truth.json as-is | Same values. |
| job and result as SDK models | job as `DocumentJob`; `result` is the raw JSON dict | The scorers read the same keys (`data`, `fields[].path`, `citations[].location`, `segments[]`, `targets[]`). |
| `job.started_at` / `finished_at` | read from the passed-through job body, if present | The console only documents `created_at`. If the others are missing, `server_*_s` is null; check `raw/` before quoting latency. |
| `credits × NDI_USD_PER_CREDIT` | `cost.total_usd` per job (`usd` in jobs.json; `usd_total`, `usd_per_doc` and `usd_settled_jobs` in the test 6 summary) | This is the actual charge, and it's null until it settles. `NDI_USD_PER_CREDIT` is no longer read. |

## 2. Parse markdown can arrive as `content_url`

**Why:** the console API's parse result has `document.markdown` *or* `document.content_url`, and the SDK says markdown can be "still being prepared" and served as a job file. Reading only `markdown` would turn a deferred document into `""`, and test 5 would report "hidden text passed to agent? no" when it hadn't checked anything. `parse_markdown()` now downloads `content_url` when `markdown` is empty and saves it next to the raw JSON as `raw/t5_parse_<file>.md`. Parse also asks for `output.formats = ["markdown"]` explicitly. That gives the same markdown as the default, without the layout blocks.

## 3. Ground results matched by target id, not position

**Why:** the old code `zip`ped the truth's targets against `result.targets` in order. If the service returned targets in a different order, each count would be scored against the wrong string. Results are now looked up by the `id` the harness sent (`g0`, `g1`, …). The expected counts are unchanged.

## 4. Mock self-test speaks the console API

**Why:** `selftest_mock.py` faked `ndi-api` endpoints. It now fakes the console's endpoints: upload grant → upload → `/v1/documents/{op}` returning a queued job → `/v1/documents/jobs/{id}`. For the injection docs it serves parse markdown only through `content_url`, so change 2 is exercised with no credits spent. The deliberate mistakes are unchanged, and the result is still 51/55 with the same 4 failures.

## Triage of the first real runs: no further harness changes

Runs `20261028-145402` (tests 1, 2, 5), `20261028-152125` (4, 7) and `20261028-152558` (6, 20 invoices). Every FAIL and job error was checked against `raw/`. None came from the harness misreading a response, so nothing above was changed because of them:

- **Polygon units, origin and field paths are read correctly.** Citations are normalized 0–1 with a top-left origin, and their rows match the truth regions. Field paths arrive as `/total`, which `field_key()` already strips.
- **Test 2, four citation FAILs (`line_item_amount`, `subtotal`, `total`, `amount_paid`): kept as FAIL; see change 5.** Extract cites the whole line (e.g. `"Total due (USD) 4,546.50"`, x 0.62–0.915) on the *correct* row. The scorer requires the polygon's centre to be inside the value's own box, and a line-wide box's centre falls outside it. Whether line-level citations should count is a change to the check, not a misread. `ground` returns tight word-level boxes for the same values.
- **Test 2, `ground "4,200.00"` FAIL (expected 2, got 3): truth.json was wrong; corrected in change 7.** The PDF prints 4,200.00 three times (unit price, line amount, subtotal; qty is 1). NDI's three matches are exactly those. `make_fixtures.py:231` noted "line amount == subtotal" and missed the unit-price column.
- **Test 4, page 6 (sideways purchase order) labelled `receipt`: real NDI miss.**
- **Test 4, receipt `4-4` FAIL: the scorer contradicted itself; resolved in change 8.** NDI returned `receipt:4-5`, absorbing the blank page. The blank-page row explicitly accepts absorption and passes, but the receipt row required the exact range 4–4, so the same behaviour was accepted in one row and failed in the other.
- **Test 7, `multipage_table.pdf` `train_job_count` 30 vs 28: real NDI miss.** The PDF has 28 `train` rows.
- **Test 7, `quote_thread.eml`: rejected before running, not scored.** `invalid_request: Cannot bound this funded input: embedded or attached image OCR has no input-size bound`. The console's format page lists `.eml` as supported for Extract, so this is the console's pre-charge check refusing an email with a PDF attachment, not an extraction result.
- **Test 5:** inline markdown, no `content_url` this time. The 4pt footnote instruction reaches the parse output; the white-text one doesn't (OCR ran on the rendered page). Extraction resisted both.
- **Known scorer weaknesses, not changed:** `matches()` would pass an empty string against any expected string (it changed no result in these runs), and the console line prints `0/0 checks` for a test-6-only run because the batch summary isn't counted.

## 5. Test 2: informational "citation on the right copy" row (scored check unchanged)

**Why:** the strict citation check (polygon centre inside the value's own box) fails a whole-line citation even when it is on the correct copy. That conflates two questions: *is the citation precise?* and *does it pick the right one of two identical values?* The strict check stays as the only pass/fail. Each field now also gets an `info` row: `yes` if some citation overlaps that field's box and none of its twins' boxes. Twins are the other fields with the same expected value (`total`↔`amount_paid`, `subtotal`↔`line_item_amount`), read from truth.json, which is not changed. The row has `pass: None`, so it is not in the check totals.

Re-scored with no API calls (change 6): `results/20261028-145402-rescored/`. Checks are **34/39, the same as the original**. The info rows are 5/5 `yes`, so every extract citation is on the right copy and none touches the twin, but only `balance_due`'s is tight enough for the strict check. In the mock, the deliberately misplaced `amount_paid` citation shows `no`.

One limit to state in the write-up: the info row doesn't check which *column*. `line_item_amount`'s citation spans the whole row, which holds 4,200.00 twice (unit price and amount), and still counts as `yes`.

## 6. `rescore` mode: score a past run again from its `raw/`

**Why:** so a scoring change can be applied to responses already paid for, and so anyone can rerun the scoring from the saved JSON. `python ndi_tests.py rescore results/<run>` replays each job from `raw/<label>.json` through the same test functions and writes `results/<run>-rescored/`, leaving the original untouched. To make that possible, the job record is now always built from the saved JSON (`job_record`), for live runs as well as replays. Test 6 keeps the original run's concurrency and batch wall time.

Checked: replaying runs `20261028-152125` (4, 7) and `20261028-152558` (6) gives reports identical to the originals apart from the title and the re-score note.

## 7. Truth correction: test 2 expects 3 matches for `ground "4,200.00"`, not 2

**This is a fix to the expected answer, not to the harness, and it was approved explicitly** because the rule for this work is that truth is never changed to make NDI pass. The evidence comes from the document, not from NDI. Text read straight from `fixtures/t2_grounding/invoice_dup_values.pdf` with `pypdf` contains `4,200.00` three times: unit price, line amount and subtotal, because the single line has qty 1. The ground check asks for every occurrence, so the correct count is 3. The old 2 came from the generator comment `# line amount == subtotal`, which missed the unit-price column. `4,546.50` (total, amount paid) is correctly 2 and is unchanged.

- `fixtures/truth.json`: only the `"4,200.00"` count changed, 2 → 3. Nothing was regenerated, so every PDF and every other truth entry is byte-for-byte what the runs used.
- `make_fixtures.py`: the same count, plus a comment naming the three copies, so a future regeneration gives the same truth.
- `selftest_mock.py`: the mock's correct ground answer for 4,200.00 is now 3 matches. Its deliberate mistakes, including the short count for 4,546.50, are unchanged, so it is still 51/55 with the same 4 failures.

Re-scored with no API calls: `results/20261028-145402-rescored/` is now **35/39** (it was 34/39 in the original run and in change 5). The only row that changed is `ground "4,200.00"`, which went from FAIL to PASS. The original `results/20261028-145402/` still shows the score under the old truth.

## 8. Test 4: a segment may absorb an adjacent blank page (pass condition changed, approved explicitly)

**Why:** the fixture's own design says the blank page should be "unclassified or absorbed, not labelled" (`make_fixtures.py:362`), and the blank-page row has always passed absorption. But the segment next to it was required to match its range exactly, so absorbing the blank page into the receipt (`receipt:4-5`) passed one row and failed the other. That contradiction has been resolved in favour of the fixture's stated design.

**The new rule** (`score_split`): a returned segment matches a true segment's range if it covers that whole range plus only pages that truth marks as blank. The class must still match. When this applies, the row's note says so (`absorbed blank page 5`), so it is visible in every report. What still fails:
- missing any page of the true range (e.g. `receipt:5-5`);
- taking a non-blank page (e.g. `receipt:4-6`, or `contract:2-4`);
- labelling the blank page as its own segment (the blank row, unchanged).

These cases were checked against the real truth with hand-made segment lists before re-scoring.

`selftest_mock.py`'s correct split answer now folds the blank page into the receipt, as the real service did, so the new path runs in the self-test. Its deliberate mistake (the rotated purchase order labelled invoice) is unchanged, and it is still 51/55 with the same 4 failures.

Re-scored with no API calls: `results/20261028-152125-rescored/` is **11/13** (the original is 10/13). The only row that changed is receipt `4-4`, which now passes with the note `absorbed blank page 5`. The sideways purchase order labelled `receipt` (page 6) is still a FAIL. The original `results/20261028-152125/` still shows the strict result.

## 9. Longer retry budget for the console's per-minute rate limit

**Why:** the first 100-invoice run (`results/20261029-214137`, don't quote it) got 19 results and 81 job errors. Every error was `429 rate_limited: Requests per minute exceeded for this account`, 74 on `upload-grants` and 7 while polling a job. None of them is an NDI result. The console's Errors page says the document routes have a per-account requests-per-minute limit, and that the SDKs retry 429s only "as long as the wait fits their 30-second retry budget". A per-minute limit's `retry-after` doesn't fit, so the SDK gave up. The 20-invoice run stayed under the limit.

The harness now creates its client with `RetryPolicy(max_retries=8, timeout=300.0)`, so it waits out `retry-after`. Retries reuse the call's idempotency key, so a retried create is never billed twice. NDI's latency figures are server-side (`created_at` → `finished_at`), so waiting on the client side doesn't change them. It does lengthen `batch_wall_s`, which now reflects the account's rate limit as well as NDI's speed.

The failed run cost $0.17 for its 19 invoices. 7 more jobs were created but their results never read, because polling hit the limit. Those are probably billed too, at about $0.06.

Rerun with the fix: `results/20261029-214329`, with 100/100 jobs succeeded and no errors. Quote its re-scored copy, which has the split from change 11.

## 11. Test 6 separates first-time parses from reused ones

**Why:** the rerun's job costs showed two prices, $0.012 for 74 invoices and $0.00885 for 26. Each job's `cost.lines` says why. For the cheaper 26, the parse line is `"execution": "reused", "reuse_reason": "reused_parse"`, at a tenth of the price. Those 26 are exactly the invoices that had already been parsed in the failed run (19 read, 7 orphaned). The service recognised the same file and reused its parse. Extraction ran fresh for all 100, so accuracy isn't affected. But the reused jobs are also faster (server run p50 2.64 s against 3.47 s), so averaging them in makes NDI look cheaper and faster than a first-time document is. The 19 invoices that succeeded in the failed run were 2-credit jobs for the same reason, reusing parses from the 20-invoice run.

- **`job_record`:** now sets `parse_reused` from the job's own cost lines.
- **Test 6 summary:** keeps every existing figure (all jobs) and adds `parse_reused_jobs` and a `fresh_parse` block with latency and $/doc for first-time parses only.
- **`frontier_compare.py`:** the side-by-side uses the `fresh_parse` figures, since the frontier model has nothing like parse reuse, and says so in the report.

Re-scored with no API calls: `results/20261029-214329-rescored/`. All 100: 100/100 invoices fully correct, end-to-end p50/p95 3.48/4.02 s, $0.01118 per invoice. First-time parses only (74): end-to-end p50/p95 3.61/4.04 s, $0.012 per invoice. Reuse also matters for the write-up as a product behaviour: sending the same file again is cheaper.

Because every fixture has now been parsed at least once, any later NDI run on these files will reuse parses. Cold-start latency and cost can't be measured again on them without new files.

## 10. `frontier_compare.py` updated for Claude Opus 5.5

**Why:** the script forced a tool call (`tool_choice: {"type": "tool"}`) to get JSON back, and Claude Opus 5.5 rejects forced tool choice with a 400. It also had `max_tokens=1024`, which thinking can use up on this model, where thinking can't be turned off.

- **JSON output:** structured output (`output_config.format`) with the same nullable schema, plus `additionalProperties: false`, which structured outputs require. The system prompt no longer mentions a tool.
- **Effort:** set explicitly to `medium`, the model's default, and reported in the header. It can be changed with `FRONTIER_EFFORT`.
- **Thinking room:** `max_tokens=16000`.
- **Refusals:** refused requests fall back to another model server-side (`fallbacks: "default"`), which is Anthropic's recommended setting. Each result records `served_by` and `fell_back`, and the report lists any invoice answered by a fallback model, so the comparison never silently mixes models. A response that doesn't end normally is recorded as an error.
- **Report:** names the NDI run its side-by-side table uses.

Scoring is unchanged: the same `score_values` and truth.

## Triage of the frontier run: no harness changes

Run `results/frontier-20261029-220953` (Claude Opus 5.5, effort `medium`): 107 requests, 0 errors, no fallback model used, $1.11 in total. One FAIL, and it is a real miss, not a misread:

- **Test 1, contract `auto_renewal`: Claude returned `false`, truth is absent (null).** The contract's only term clause is "continues for twenty-four (24)" months, with nothing about renewal. The prompt says to use null for any field the document doesn't state. Claude inferred "doesn't renew" instead. NDI returned `not_found` for the same field. Kept as FAIL.

The latencies in the side-by-side aren't measured the same way. NDI's is server time from queued to finished, which leaves out upload and polling. Claude's is the whole request's wall clock. The difference favours NDI, so Claude's lead on latency is if anything understated.

## Triage of the `parse_mode high` rerun (tests 2, 4, 7): no harness changes

Run `results/20261030-103747`: 20/25 checks, $0.189. Every parse was fresh (no `reused` cost lines), because a different parse mode can't reuse the low-mode parse. Compared with the low-mode runs:

- **Test 4, sideways purchase order (page 6): fixed in high mode.** It is now `purchase_order:6-6`, so all 5 split rows pass, the receipt again absorbing the blank page (change 8). In low mode it was labelled `receipt`.
- **Test 2 citations: unchanged.** Still 1/5 on the strict check and 5/5 on the right copy. Extraction still cites whole lines. Values and `ground` all pass, as in low mode.
- **Test 7, `train_job_count`: still 30, the PDF has 28.** The same miss in both modes. Its only citation is `source_text: "30"` with an empty `polygons` list. Page 1 has two cells reading 30, so the citation points at no location, and the value doesn't appear to be a count of rows. The xlsx checks pass, as in low mode.
- **Test 7, `quote_thread.eml`: rejected before running again**, with the same `Cannot bound this funded input` error and no charge.
- **Latency is much higher in high mode:** server run time was 15.3 s for test 2's extract (one page), 66.5 s for the 6-page split, and 126.1 s for the 2-page table. This is page verification and correction at work. Quote it next to any high-mode accuracy gain.

## 12. Document AI comparison (`docai_compare.py`), scored by the NDI checks

**Why:** the only like-for-like test of NDI's citation claim is another product that returns field locations. Claude returns values only.

**How it runs:** Google Document AI, zero-shot, with no training.
- **Extraction:** a Custom Extractor gets each test's JSON schema as a per-request `schema_override`.
- **Splitting:** a Custom Splitter gets the test's 4 classes and their descriptions.
- **Versions:** the Gemini 3 previews, `pretrained-foundation-model-v1.6-pro-2025-12-01` (Gemini 3 Pro) and `pretrained-splitter-v1.6-pro-2026-03-09` (Gemini 3.1 Pro). The user chose these over the GA v1.5 versions. The report names both.
- **A gate:** two documents run first, and the script stops if the output isn't the expected shape.

**Same scorer:** each Document AI response is converted to NDI's job shape and saved in `raw/`. The untouched responses are kept in `docai_raw/`. Then `ndi_tests.replay_tests` scores it. Nothing in `scoring.py` or truth.json changed for this. The conversion:
- An entity's `pageAnchor` box becomes a citation. Pages go from 0-based to 1-based.
- A field with no entity becomes `not_found`.
- Split entities' page refs become page ranges.
- Test 5's hidden-text check reads the extractor response's `document.text`, the text an agent would get.

**Choices that could affect the result, stated so they can be questioned:**
- **Derive vs Extract:** booleans and integer fields named `*_count` (`line_item_count`, `row_count`, `train_job_count`) use Document AI's Derive method, because they aren't printed on the page. Derived fields come back without a location; none of them has a citation check.
- **Latency:** the request's wall clock, like Claude's. Quota errors are retried, and only the successful attempt is timed. The number of retries is kept in `jobs.json`.
- **Cost:** list price per page ($30 or $5 per 1,000, read 2026-10-30), not a billed amount. The report's "USD charged" line for this run means list price.
- **Not run, and shown as not run rather than failed:** `ground` (no equivalent), and the xlsx and .eml fixtures (not Document AI input types).
- **Numbers are read from Google's normalized text, not `float_value`** (fixed after the gate, 2026-10-31). `float_value` is a 32-bit proto float, so the gate's total 6,720.53 was read as 6720.5298. The scorer rounds to cents, so this one would still have passed, but from about $100,000 a float32 can be off by a cent or more. `normalizedValue.text` ("6720.53") is Google's own normalized answer, so reading it doesn't change what Document AI said. `float_value` is the fallback when the text doesn't parse.

**Changes to `ndi_tests.py`, all reporting only:**
- `Run` takes a report title and API line.
- `rescore` keeps the source run's title and API line.
- The replay loop is now `replay_tests()`, shared with `docai_compare.py`. Replaying `20261028-152558` gives the same scores.
- Test 2 now shows a "not run" row when `ground` doesn't run, instead of dropping it silently. That applies to NDI too.

**Checked offline:** `python docai_compare.py --selftest` uses a fake Document AI (`selftest_docai.py`) built from truth.json in Document AI's own response types. It plants three mistakes: amount_paid cites its twin, inv_007's total is off by a cent, and the rotated purchase order is labelled invoice. It gets exactly those: 44/46 checks, 99/100 invoices. The NDI mock is still 51/55.

## 13. Document AI number fallback misread "forty-five (45)" as -45

**Found in:** the full run `docai-20261031-202256`. The contract's `payment_terms_days` came back as an entity with mention text `forty-five (45)` and no normalized value.

**The bug:** the fallback removed every character except digits, `.` and `-`, so the hyphen in "forty-five" was kept and the value became -45. Document AI had read the right clause; the harness misparsed it.

**Fix:** the fallback now takes the first number in the text (`45`). Thousands commas are dropped (`$1,234.50` → 1234.5).

**Re-scored, not re-run:** a new `--rescore <run>` option rebuilds `raw/` from the saved `docai_raw/` responses into `<run>-rescored`. It keeps each job's timings and list-price cost and sends nothing to Google. `docai-20261031-202256-rescored` differs from the original in that row alone: 42/46 → 43/46. The self-test is unchanged at 44/46.

## Triage of the Document AI run: no other harness changes

Quote `results/docai-20261031-202256-rescored`. The other three failures and the 10 invoice misses are Document AI's own, checked against `docai_raw/`:
- **Test 7, table counts** (38 of 75 rows, 14 of 28 train, max GPUs 61 of 63): the response has both pages, but all three answers match page 1 alone (38 rows, 15 train). It counted one page.
- **Test 6, `invoice_date`, 9 invoices** (inv_002, 006, 043, 053, 055, 068, 084, 086, 091): no `invoice_date` entity came back at all. Each date is printed in ISO under "Date:", the same layout as the 91 it found.
- **Test 6, `vendor_name`, inv_018:** it returned the bill-to customer (Halvard Robotics Inc.) instead of the vendor (Pallas Cloud Storage).

## Other files

- `requirements.txt`: `ndi-sdk>=0.39,<0.40` → `nace-sdk==0.4.0`, pinned because it's 0.x.
- `frontier_compare.py`: the side-by-side "$ per doc" fallback now says "not settled" instead of "set NDI_USD_PER_CREDIT". Nothing else changed.
- `README.md`: new key and endpoint, workspace persistence, and the latency and cost caveats above.
