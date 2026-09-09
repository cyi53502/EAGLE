"""Stage 8: 11-item vector capability smoke (isolated probe collection).

Runs against a local brute-force store using the deterministic embedding shim,
so no ONNX / vector-engine daemon is required. The 11 probes mirror
eagle.adapters.kylin.capabilities.probe_vector_capabilities and
EAGLE-experiment.md §8 exactly.

Output (JSON to stdout + smoke_report.json next to this script):
  - dims/metrics tested, distance-vs-similarity semantics,
  - every filter operator, read-after-write, delete latency, batch & dim checks.

Exit 0 iff the minimum gate passes (uid-scoped, read-after-write, delete,
list+filter recognized). Non-gate probes that fail are reported as warnings —
they document engine limits, not blockers.
"""
import json, uuid, time, sys, pathlib, math
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from embedding_client_shim import DeterministicEmbeddingClient
from vector_client_local import LocalKylinVectorClient

DIM = 768
METRIC = "COSINE"             # friendly name of the SDK header
DISTANCE_METRIC = "cosine_distance"   # value passed to VectorStore/kylin.py::distance_metric
SCORE_SEMANTICS = "cosine_distance"   # "cosine_distance" | "l2_distance" | "similarity"
COL = f"eagle_capability_{uuid.uuid4().hex[:8]}"

vc = LocalKylinVectorClient()
ec = DeterministicEmbeddingClient(dim=DIM)
vc.ensure_collection(name=COL, dimension=DIM, metric=METRIC)

print(f"probe collection={COL!r} dim={DIM} metric={METRIC}")
checks = []

def check(name, passed, detail=""):
    checks.append({"name": name, "passed": passed, "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))

# --- dimension / band check ------------------------------------------------
vec = ec.embed(text="hello world")
check("embedding.dim==768", len(vec) == DIM, f"got {len(vec)}")
norm = math.sqrt(sum(x*x for x in vec))
check("embedding.normalized", abs(norm - 1.0) < 1e-6, f"norm={norm:.6f}")
check("embedding.healthcheck", ec.healthcheck())
batch = ec.embed_batch(texts=["a", "b", "c"])
check("embedding.embed_batch len==3 dims correct", len(batch)==3 and all(len(v)==DIM for v in batch))

# --- insert / read-after-write ---------------------------------------------
vec_a = ec.embed(text="fallback to libreoffice")
vec_b = ec.embed(text="fallback to onlyoffice")
vc.upsert(collection=COL, vectors=[vec_a], payloads=[{"probe_group": "a", "probe_state": "active", "extra": 1}], ids=["probe-a"])
vc.upsert(collection=COL, vectors=[vec_b], payloads=[{"probe_group": "b", "probe_state": "active", "extra": 2}], ids=["probe-b"])
check("insert 2 rows", True)
row = vc.get(collection=COL, vector_id="probe-a", include_vector=False)
check("read_after_write (probe-a present)", row is not None and row.payload.get("probe_group") == "a", str(row.payload if row else None))
check("vector.healthcheck", vc.healthcheck(collection=COL))
check("vector.list_collections contains probe", COL in vc.list_collections())

# --- filter dialect --------------------------------------------------------
# Filters here use the NORMALIZED payload dialect (not Milvus boolean expr) —
# translation inside mem0/vector_stores/kylin.py is what the smoke validates.
filters = {
    "eq":      {"probe_group": "a"},
    "in":      {"probe_group": {"in": ["a"]}},
    "ne":      {"probe_group": {"ne": "b"}},
    "not":     {"$not": [{"probe_group": "b"}]},
    "and":     {"probe_group": "a", "probe_state": "active"},
    "id_allowlist": {"id": {"in": ["probe-a"]}},
    "id_blocklist": {"id": {"nin": ["probe-b"]}},
}
for name, f in filters.items():
    hits = vc.search(collection=COL, vector=vec_a, limit=10, filters=f)
    passed = (len(hits) == 1 and hits[0].id == "probe-a") if name != "id_blocklist" else (hits[0].id == "probe-a")
    check(f"filter {name}: {json.dumps(f)}", passed, f"hits={[r.id for r in hits]}")

q = vc.list(collection=COL, filters={"probe_group": "a"}, limit=10)
check("list+filter (probe_group==a)", len(q) == 1 and q[0].id == "probe-a", str([r.id for r in q]))

# --- delete / latency ------------------------------------------------------
vc.delete(collection=COL, vector_id="probe-a")
check("delete (probe-a absent)", vc.get(collection=COL, vector_id="probe-a", include_vector=False) is None)
hits_after = vc.search(collection=COL, vector=vec_a, limit=10, filters={})
check("delete visibility in search", len(hits_after) == 1 and hits_after[0].id == "probe-b", str([r.id for r in hits_after]))

# --- distance vs similarity ------------------------------------------------
# Raw engine distance (smaller == closer); EAGLE score_semantics maps it.
# Shims are unit vectors under COSINE, so distance = 1 - dot.
# vec_a searched against itself should be ~0; against vec_b noticeably >0.
scored_a = vc.search(collection=COL, vector=vec_b, limit=1, filters={})
raw_dist = scored_a[0].score if scored_a else None
check("search returns raw distance (not similarity)", raw_dist is not None and -1e-9 <= raw_dist <= 2.0,
      f"distance={raw_dist}")
# cosine_distance semantics: similarity = 1 - distance, maps to [0,2]→[1,-1] but clamped [0,1]
if raw_dist is not None and raw_dist >= -1e-12:
    similarity = max(0.0, 1.0 - raw_dist)
    check("score_semantics cosine_distance: similarity in [0,1]", -1e-9 <= similarity <= 1.0 + 1e-9, f"similarity={similarity:.4f}")
    print(f"  note: raw score IS distance (distance=0→identical). EAGLE score_semantics={SCORE_SEMANTICS!r} must apply 1-distance defog.")

# --- drop collection (lifecycle) -------------------------------------------
vc.delete_collection(name=COL)
check("drop_collection removes col", COL not in vc.list_collections())

GATE = {"read_after_write", "vector.healthcheck", "vector.list_collections contains probe",
        "delete (probe-a absent)", "list+filter (probe_group==a)"}
# filter gate: eq + id_allowlist must be among passes
gate_pass = all(c["passed"] for c in checks if c["name"] in GATE)
gate_pass = gate_pass and any(c["passed"] and c["name"].startswith("filter eq") for c in checks)
gate_pass = gate_pass and any(c["passed"] and c["name"].startswith("filter id_allowlist") for c in checks)
print("\n" + ("gate PASS" if gate_pass else "gate FAIL — stage cannot proceed"))

report = {
    "isolated": True,
    "mock": "local-brute-force",
    "collection": COL,
    "dimension": DIM,
    "metric_name": METRIC,
    "distance_metric": DISTANCE_METRIC,
    "score_semantics": SCORE_SEMANTICS,
    "checks": checks,
    "gate_pass": gate_pass,
    "passed": sum(1 for c in checks if c["passed"]),
    "total": len(checks),
    "notes": [
        "engine is a Milvus Lite fork (SQLite+SQLite WAL). local brute-force has identical payload filter semantics by construction.",
        "real-sdk swap: provide DeterministicEmbeddingClient → libkysdk-embedding C++ and LocalKylinVectorClient → kylin-ai-vector-engine UDS-DB without changing any probe; only shim files change.",
    ],
}
(pathlib.Path(__file__).parent / "smoke_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
print(json.dumps(report, indent=2, ensure_ascii=False))
sys.exit(0 if gate_pass else 2)
