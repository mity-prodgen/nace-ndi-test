"""Same documents, schemas and scorer as ndi_tests.py, sent to Google Document AI.

    gcloud auth application-default login
    export DOCAI_PROJECT=your-gcp-project-id
    python docai_compare.py --gate-only        # two documents, checks the output shape, ~$0.06
    python docai_compare.py [--batch-n 100]     # everything, ~$3.50
    python docai_compare.py --selftest          # fake Document AI, no GCP, no cost

Zero-shot, no training: Custom Extractor (generative) with the test's JSON schema sent as a
per-request schema override, and Custom Splitter with the test's classes. Each answer is saved
as an NDI-shaped job body in raw/, then scored by ndi_tests.replay_tests, so Document AI goes
through exactly the same checks as NDI. The untouched Document AI responses are kept in docai_raw/.

Not comparable, so recorded as unsupported rather than failed: `ground` (no equivalent), and the
xlsx and .eml fixtures (not Document AI input types).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import ndi_tests as nt

ROOT = Path(__file__).parent
FIX = ROOT / "fixtures"
PROJECT = os.environ.get("DOCAI_PROJECT")
LOCATION = os.environ.get("DOCAI_LOCATION", "us")
# Preview versions on Gemini 3: the user's choice over the GA v1.5 versions (2026-10-30)
EXTRACTOR_VERSION = os.environ.get("DOCAI_EXTRACTOR_VERSION", "pretrained-foundation-model-v1.6-pro-2025-12-01")  # Gemini 3 Pro
SPLITTER_VERSION = os.environ.get("DOCAI_SPLITTER_VERSION", "pretrained-splitter-v1.6-pro-2026-03-09")  # Gemini 3.1 Pro
# List prices from cloud.google.com/document-ai/pricing, read 2026-10-30, first-tier rates per page
USD_PER_PAGE = {"extract": 30 / 1000, "split": 5 / 1000}
VALUE_TYPES = {"string": "string", "number": "number", "integer": "number", "boolean": "boolean"}


def derived(name: str, spec: dict) -> bool:
    """Counts and yes/no answers aren't printed on the page, so they use Document AI's Derive method."""
    return spec.get("type") == "boolean" or (spec.get("type") == "integer" and name.endswith("_count"))


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def extractor_schema(schema: dict):
    from google.cloud import documentai_v1beta3 as dai
    P = dai.DocumentSchema.EntityType.Property
    props = [P(name=k, description=v.get("description", ""), value_type=VALUE_TYPES[v["type"]],
               occurrence_type=P.OccurrenceType.OPTIONAL_ONCE,
               method=P.Method.DERIVE if derived(k, v) else P.Method.EXTRACT)
             for k, v in schema["properties"].items()]
    return dai.DocumentSchema(entity_types=[dai.DocumentSchema.EntityType(
        name="custom_extraction_document_type", base_types=["document"], properties=props)])


def splitter_schema(classes: list[dict]):
    from google.cloud import documentai_v1beta3 as dai
    return dai.DocumentSchema(
        entity_types=[dai.DocumentSchema.EntityType(name=c["id"], display_name=c["label"],
                                                    description=c.get("description", ""), base_types=["document"])
                      for c in classes],
        metadata=dai.DocumentSchema.Metadata(document_splitter=True))


class DocAI:
    """One extractor and one splitter processor, created on first use and reused by display name."""

    def __init__(self):
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai_v1beta3 as dai
        self.dai = dai
        self.c = dai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com"))
        parent = f"projects/{PROJECT}/locations/{LOCATION}"
        have = {p.display_name: p for p in self.c.list_processors(parent=parent)}
        self.version = {}
        for op, type_, ver in [("extract", "CUSTOM_EXTRACTION_PROCESSOR", EXTRACTOR_VERSION),
                               ("split", "CUSTOM_SPLITTING_PROCESSOR", SPLITTER_VERSION)]:
            name = f"ndi-compare-{op}"
            proc = have.get(name) or self.c.create_processor(
                parent=parent, processor=dai.Processor(type_=type_, display_name=name))
            self.version[op] = f"{proc.name}/processorVersions/{ver}"
            try:
                self.c.get_processor_version(name=self.version[op])
            except Exception as e:  # noqa: BLE001 - say which versions exist instead
                avail = [v.name.rsplit("/", 1)[-1] for v in self.c.list_processor_versions(parent=proc.name)]
                sys.exit(f"{op}: version {ver} not available ({type(e).__name__}). Available: {avail}")

    def process(self, op: str, path: Path, schema) -> tuple:
        """One online request. Quota errors are retried; the latency kept is the successful attempt's."""
        from google.api_core import exceptions as gexc
        req = self.dai.ProcessRequest(
            name=self.version[op],
            raw_document=self.dai.RawDocument(content=path.read_bytes(), mime_type="application/pdf"),
            process_options=self.dai.ProcessOptions(schema_override=schema))
        for attempt in range(8):
            started = datetime.now(timezone.utc)
            t0 = time.perf_counter()
            try:
                doc = self.c.process_document(request=req, timeout=600).document
                return doc, started, datetime.now(timezone.utc), round(time.perf_counter() - t0, 3), attempt
            except (gexc.ResourceExhausted, gexc.ServiceUnavailable):
                time.sleep(min(60, 4 * 2 ** attempt))
        raise RuntimeError("still over quota after 8 attempts")


# ---------------------------------------------------------------------------
# Document AI response -> the job body shape ndi_tests scores
# ---------------------------------------------------------------------------
def flat(entities):
    for e in entities:
        yield e
        yield from flat(e.properties)


def value_of(e, json_type: str):
    nv = e.normalized_value
    kind = nv._pb.WhichOneof("structured_value") if nv else None
    if json_type == "boolean":
        if kind == "boolean_value":
            return nv.boolean_value
        t = (nv.text or e.mention_text).strip().lower()
        return {"true": True, "yes": True, "false": False, "no": False}.get(t)
    if json_type in ("number", "integer"):
        if kind == "money_value":
            n = nv.money_value.units + nv.money_value.nanos / 1e9
        elif kind == "integer_value":
            n = nv.integer_value
        elif kind == "float_value":
            # float_value is a proto float32 (6720.53 -> 6720.5298); Google's normalized text keeps the exact digits
            try:
                n = float(nv.text)
            except ValueError:
                n = nv.float_value
        else:
            # first number in the text: "forty-five (45)" -> 45, "$1,234.50" -> 1234.5 (stripping non-digits gave -45)
            m = re.search(r"-?\d[\d,]*(?:\.\d+)?", nv.text or e.mention_text)
            if not m:
                return e.mention_text
            n = float(m.group().replace(",", ""))
        return int(n) if json_type == "integer" and float(n).is_integer() else n
    return e.mention_text or nv.text


def extract_body(doc, schema: dict) -> dict:
    ents = list(flat(doc.entities))
    data, fields = {}, []
    for name, spec in schema["properties"].items():
        hits = sorted((e for e in ents if e.type_ == name or e.type_.endswith("/" + name)),
                      key=lambda e: -e.confidence)
        if not hits:
            data[name] = None
            fields.append({"path": f"/{name}", "value": None, "status": "not_found", "citations": []})
            continue
        e = hits[0]
        data[name] = value_of(e, spec["type"])
        cites = [{"location": {"kind": "page_region", "page": ref.page + 1,  # Document AI pages are 0-based
                               "polygons": [[[v.x, v.y] for v in ref.bounding_poly.normalized_vertices]]
                               if ref.bounding_poly.normalized_vertices else []},
                  "source_text": e.mention_text}
                 for ref in e.page_anchor.page_refs]
        fields.append({"path": f"/{name}", "value": data[name], "status": "found",
                       "confidence": e.confidence, "citations": cites})
    return {"result_type": "extract", "data": data, "fields": fields}


def split_body(doc) -> dict:
    segs = []
    for e in doc.entities:
        pages = sorted(ref.page + 1 for ref in e.page_anchor.page_refs)
        if pages:
            segs.append({"status": "matched", "class": {"id": e.type_}, "start_page": pages[0],
                         "end_page": pages[-1], "classification_confidence": {"band": round(e.confidence, 2)}})
    segs.sort(key=lambda s: s["start_page"])
    return {"result_type": "split", "segments": segs}


def job_body(label: str, op: str, result: dict, started, finished, pages: int, usd: float) -> dict:
    iso = lambda t: t.isoformat()
    return {"job_id": f"docai-{label}", "status": "succeeded", "error": None, "units": pages, "credits": None,
            # synchronous call: queued = started, so server_total_s is the request's wall clock, like Claude's
            "created_at": iso(started), "started_at": iso(started), "finished_at": iso(finished),
            "cost": {"total_usd": f"{usd:.5f}", "lines": [{"operation": op, "quantity": pages, "unit": "page",
                                                          "execution": "fresh", "list_price": True}]},
            "result": result}


def unsupported(label: str, op: str, path: Path, why: str) -> dict:
    return {"label": label, "op": op, "file": path.name, "ok": False, "error": f"not run on Document AI: {why}"}


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def plan(T: dict, batch_n: int) -> list[tuple]:
    """(label, op, path, schema-or-classes) for every job, using ndi_tests' labels."""
    jobs = []
    for name, case in T["t1_absent"].items():
        jobs.append((f"t1_{name}", "extract", FIX / "t1_absent" / name, case["schema"]))
    for name, case in T["t2_grounding"].items():
        jobs.append((f"t2_extract_{name}", "extract", FIX / "t2_grounding" / name, case["schema"]))
        jobs.append((f"t2_ground_{name}", "ground", FIX / "t2_grounding" / name, None))
    for name, case in T["t4_packet"].items():
        jobs.append((f"t4_split_{name}", "split", FIX / "t4_packet" / name, case["classes"]))
    for name, case in T["t5_injection"].items():
        jobs.append((f"t5_extract_{name}", "extract", FIX / "t5_injection" / name, case["schema"]))
    for name, case in T["t7_odd"].items():
        jobs.append((f"t7_{name}", "extract" if name.endswith(".pdf") else "unsupported", FIX / "t7_odd" / name,
                     case["schema"]))
    for name in sorted(T["t6_batch"]["docs"])[:batch_n]:
        jobs.append((f"t6_{name}", "extract", FIX / "t6_batch" / name, T["t6_batch"]["schema"]))
    return jobs


def run_one(api, run_dir: Path, job: tuple) -> dict:
    label, op, path, spec = job
    raw, native = run_dir / "raw", run_dir / "docai_raw"
    if op == "ground":
        rec = unsupported(label, op, path, "Document AI has no equivalent of ground")
    elif op == "unsupported":
        rec = unsupported(label, op, path, f"{path.suffix} is not a Document AI input type")
    else:
        try:
            schema = extractor_schema(spec) if op == "extract" else splitter_schema(spec)
            doc, started, finished, wall, retries = api.process(op, path, schema)
        except Exception as e:  # noqa: BLE001 - record and keep going
            rec = {"label": label, "op": op, "file": path.name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            (native / f"{label}.json").write_text(type(doc).to_json(doc))
            pages = len(doc.pages) or 1
            result = extract_body(doc, spec) if op == "extract" else split_body(doc)
            body = job_body(label, op, result, started, finished, pages, pages * USD_PER_PAGE[op])
            (raw / f"{label}.json").write_text(json.dumps(body, indent=2))
            if label.startswith("t5_extract_"):  # the text an agent would read, for test 5's hidden-text check
                parse = job_body(label.replace("t5_extract_", "t5_parse_"), "parse",
                                 {"result_type": "parse", "document": {"markdown": doc.text}}, started, finished, 0, 0)
                (raw / f"{label.replace('t5_extract_', 't5_parse_')}.json").write_text(json.dumps(parse, indent=2))
            return {"label": label, "client_wall_s": wall, "retries": retries}
    (raw / f"{label}.json").write_text(json.dumps(rec, indent=2))
    return {"label": label, "client_wall_s": None}


def gate(api, run_dir: Path, T: dict) -> tuple[bool, list[dict]]:
    """Two documents before spending on the rest: is the output the shape the conversion expects?"""
    t2 = next(iter(T["t2_grounding"]))
    ok, walls = True, []
    for job in [("t6_inv_000.pdf", "extract", FIX / "t6_batch" / "inv_000.pdf", T["t6_batch"]["schema"]),
                (f"t2_extract_{t2}", "extract", FIX / "t2_grounding" / t2, T["t2_grounding"][t2]["schema"])]:
        w = run_one(api, run_dir, job)
        walls.append(w)
        body = json.loads((run_dir / "raw" / f"{job[0]}.json").read_text())
        if "job_id" not in body:
            print(f"  gate {job[0]}: {body['error']}"); ok = False; continue
        fields = body["result"]["fields"]
        found = [f for f in fields if f["status"] == "found"]
        boxed = [f for f in found if any(c["location"]["polygons"] for c in f["citations"])]
        print(f"  gate {job[0]}: {len(found)}/{len(fields)} fields found, {len(boxed)} with boxes, "
              f"{w['client_wall_s']} s, data={body['result']['data']}")
        ok &= bool(found)
    return ok, walls


def three_way(run_dir: Path) -> list[str]:
    """NDI, Claude and Document AI on the same invoices, each from its own latest run."""
    def summ(path):
        return json.loads(path.read_text()) if path and path.exists() else None
    ndi_p = sorted((ROOT / "results").glob("2*/raw/t6_summary.json"))
    ndi = summ(ndi_p[-1]) if ndi_p else None
    fr = sorted((ROOT / "results").glob("frontier-*/results.json"))
    claude = None
    if fr:
        b = [x for x in json.loads(fr[-1].read_text()) if x["section"] == "6" and "error" not in x]
        w = sorted(x["wall_s"] for x in b)
        q = lambda p: w[min(len(w) - 1, int(round(p * (len(w) - 1))))]
        claude = {"ok": f"{sum(all(r['pass'] for r in x['rows']) for x in b)}/{len(b)}", "p50": q(.5), "p95": q(.95),
                  "usd": round(statistics.mean(x["usd"] for x in b), 5) if all(x.get("usd") for x in b) else None}
    me = summ(run_dir / "raw" / "t6_summary.json")
    if not (ndi and me):
        return []
    f = ndi.get("fresh_parse") or ndi
    rows = [("all fields correct", ndi["doc_accuracy_all_fields"], claude and claude["ok"], me["doc_accuracy_all_fields"]),
            ("p50 latency (s)", f["end_to_end_p50_s"], claude and claude["p50"], me["end_to_end_p50_s"]),
            ("p95 latency (s)", f["end_to_end_p95_s"], claude and claude["p95"], me["end_to_end_p95_s"]),
            ("$ per doc", f["usd_per_doc"], claude and claude["usd"], me["usd_per_doc"])]
    out = ["## NDI vs Claude vs Document AI, same invoices", "",
           f"NDI from `{ndi_p[-1].parent.parent.relative_to(ROOT)}` (first-time parses), "
           f"Claude from `{fr[-1].parent.relative_to(ROOT) if fr else 'none'}`. "
           "NDI latency is server time, queued to finished; Claude and Document AI latency is request wall clock. "
           "Document AI $ is list price, not a billed amount.", "",
           "| | NDI | Claude | Document AI |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c if c is not None else '—'} | {d} |" for a, b, c, d in rows]
    return out + [""]


def rescore(src: Path):
    """Rebuild raw/ from the saved Document AI responses into <src>-rescored. Timings and costs are kept; nothing is sent."""
    from google.cloud import documentai_v1beta3 as dai
    T = nt.truth()
    run_dir = src.parent / f"{src.name}-rescored"
    shutil.copytree(src / "docai_raw", run_dir / "docai_raw")
    (run_dir / "raw").mkdir()
    shutil.copy(src / "jobs.json", run_dir / "jobs.json")
    specs = {label: (op, spec) for label, op, _, spec in plan(T, 10**6)}
    for old in sorted((src / "raw").glob("*.json")):
        body = json.loads(old.read_text())
        native = src / "docai_raw" / old.name
        if native.exists():
            op, spec = specs[old.stem]
            doc = dai.Document.from_json(native.read_text(), ignore_unknown_fields=True)
            body["result"] = extract_body(doc, spec) if op == "extract" else split_body(doc)
        (run_dir / "raw" / old.name).write_text(json.dumps(body, indent=2))
    old_report = (src / "report.md").read_text()
    head = old_report.splitlines()
    t6 = json.loads(re.search(r"```json\n(.*?)\n```", old_report, re.S).group(1))
    run = nt.Run("n/a", name=run_dir.name, label="Document AI comparison",
                 api=next(l for l in head if l.startswith("API: "))[5:].rstrip(),
                 note=f"Re-scored from {src.name}'s saved Document AI responses; nothing re-sent. "
                      "Zero-shot, schema sent per request. Scored by ndi_tests.py's own checks. "
                      "$ is list price per page. Unsupported jobs (ground, xlsx, .eml) show as failed jobs.")
    nt.replay_tests(run_dir, run, t6.get("concurrency"), t6.get("batch_wall_s"))
    nt.write_report(run)
    extra = three_way(run_dir)
    if extra:
        with open(run_dir / "report.md", "a") as fh:
            fh.write("\n".join(extra))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--gate-only", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="fake Document AI; no GCP, no cost")
    ap.add_argument("--rescore", type=Path, help="re-score a finished run from its saved responses; nothing is sent")
    a = ap.parse_args()
    if a.rescore:
        return rescore(a.rescore)
    if a.selftest:
        from selftest_docai import FakeDocAI as Api
    else:
        if not PROJECT:
            sys.exit("Set DOCAI_PROJECT to your GCP project id, and run: gcloud auth application-default login")
        Api = DocAI
    api = Api()
    T = nt.truth()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "results" / f"docai-{stamp}{'-selftest' if a.selftest else ''}"
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "docai_raw").mkdir()

    print("gate: two documents first")
    passed, walls = gate(api, run_dir, T)
    if not passed:
        sys.exit(f"Gate failed; nothing else was sent. Raw responses: {run_dir / 'docai_raw'}")
    if a.gate_only:
        print(f"Gate passed. Raw responses: {run_dir / 'docai_raw'}"); return

    done = {w["label"] for w in walls}  # the gate's two documents aren't sent again
    jobs = [j for j in plan(T, a.batch_n) if j[0] not in done]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.concurrency) as ex:
        for i, w in enumerate(ex.map(lambda j: run_one(api, run_dir, j), jobs), 1):
            walls.append(w)
            print(f"  {i}/{len(jobs)}", end="\r")
    batch_wall = round(time.perf_counter() - t0, 1)
    print()
    (run_dir / "jobs.json").write_text(json.dumps(walls, indent=2))

    ver = "selftest" if a.selftest else version("google-cloud-documentai")
    run = nt.Run("n/a", name=run_dir.name, label="Document AI comparison",
                 api=f"Google Document AI ({LOCATION}), extractor `{EXTRACTOR_VERSION}`, splitter `{SPLITTER_VERSION}`, "
                     f"google-cloud-documentai {ver}",
                 note="Zero-shot, schema sent per request. Scored by ndi_tests.py's own checks. "
                      "$ is list price per page. Unsupported jobs (ground, xlsx, .eml) show as failed jobs.")
    # test 6's wall time covers the whole job list, which is mostly the batch
    nt.replay_tests(run_dir, run, a.concurrency, batch_wall)
    nt.write_report(run)
    extra = three_way(run_dir)
    if extra:
        with open(run_dir / "report.md", "a") as fh:
            fh.write("\n".join(extra))


if __name__ == "__main__":
    main()
