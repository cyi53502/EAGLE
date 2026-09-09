"""Stage 8: 11-item vector capability smoke against the live Kylin engine (Milvus Lite fork).

The engine listens on UDS unix:/tmp/kylin-ai-vector-engine-0.sock (from
kylin-ai-vector-engine-default). pymilvus MilvusClient speaks the same Milvus
gRPC protocol; we probe by trying several URI forms before giving up.
"""
import uuid, time, sys, traceback, json

UDS = "/tmp/kylin-ai-vector-engine-0.sock"

def connect(uri):
    from pymilvus import MilvusClient
    c = MilvusClient(uri=uri)
    # force a list-collections RPC to verify the channel
    c.list_collections()
    return c

URIS = [
    f"http://unix:{UDS}",
    f"unix://{UDS}",
    f"unix:///{UDS.lstrip('/')}",
    UDS,
]

client = None
for uri in URIS:
    try:
        print(f"TRY uri={uri!r} ...", end=" ")
        client = connect(uri)
        print("OK")
        ACTIVE_URI = uri
        break
    except Exception as e:
        print(f"FAIL {type(e).__name__}: {e}")

if client is None:
    print("\n--- last error full traceback for first URI ---")
    try:
        connect(URIS[0])
    except Exception:
        traceback.print_exc()
    sys.exit(2)

print(f"\nACTIVE_URI={ACTIVE_URI!r}")

DIM = 8
COL = f"eagle_capability_{uuid.uuid4().hex[:8]}"
print(f"collection={COL!r} dim={DIM}")

def report(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))

# ensure clean slate
if client.has_collection(COL):
    client.drop_collection(COL)

# probe: create_collection + metric enumeration + RW/ops discovered while running
from pymilvus import MilvusClient

# create with COSINE metric
client.create_collection(COL, dimension=DIM, metric_type="COSINE", auto_id=False)
print(f"created {COL!r} COSINE")
# candidate metrics the header exposes
for metric in ("COSINE", "L2", "IP"):
    col2 = f"{COL}_{metric.lower()}"
    try:
        client.create_collection(col2, dimension=DIM, metric_type=metric, auto_id=False)
        client.drop_collection(col2)
        report(f"metric {metric}", True)
    except Exception as e:
        report(f"metric {metric}", False, str(e)[:120])

print("\n--- insert / RAW probes ---")
import random
random.seed(42)
def rand_vec():
    return [random.random() for _ in range(DIM)]

rows_a = [{"id": 1, "vector": rand_vec(), "probe_group": "a", "probe_state": "active", "extra": 1},
          {"id": 2, "vector": rand_vec(), "probe_group": "b", "probe_state": "active", "extra": 2}]
# milvus lite python API: insert expects list of dicts with id+vector (+ scalars become dynamic fields if enabled)
# Try MilvusClient.insert first
try:
    ins = client.insert(COL, data=rows_a)
    print(f"insert OK: {ins}")
    report("insert 2 rows", True)
except Exception as e:
    print(f"insert FAIL: {e}")
    traceback.print_exc()
    sys.exit(3)

# read-after-write: get by id (query)
try:
    q = client.query(COL, filter="id in [1]", output_fields=["probe_group", "probe_state"])
    report("read_after_write (query id=1)", len(q) == 1 and q[0].get("probe_group") == "a", str(q)[:200])
except Exception as e:
    report("read_after_write", False, str(e)[:120])

# search with different filter dialects (the Milvus boolean expr language)
filters = {
    "eq":      'probe_group == "a"',
    "in":      'probe_group in ["a"]',
    "ne":      'probe_group != "b"',
    "not":     'not (probe_group == "b")',
    "and":     'probe_group == "a" and probe_state == "active"',
    "id_allowlist": 'id in [1]',
    "id_blocklist": 'id not in [2]',
}

for name, expr in filters.items():
    try:
        r = client.search(COL, data=[rand_vec()], filter=expr, limit=5, output_fields=["probe_group"])
        # pymilvus search returns list[list[dict]]
        hits = r[0] if r else []
        report(f"filter {name}: {expr!r}", True, f"hits={len(hits)}")
    except Exception as e:
        report(f"filter {name}", False, str(e)[:140])

# list/query + filter (the list API in pymilvus is query)
try:
    q2 = client.query(COL, filter='probe_group == "a"', output_fields=["probe_group"])
    report("list_filter (query)", len(q2) >= 1)
except Exception as e:
    report("list_filter", False, str(e)[:120])

# delete + visibility latency: delete id=1, then query should be empty
try:
    client.delete(COL, filter="id in [1]")
    time.sleep(0.3)
    q3 = client.query(COL, filter="id in [1]", output_fields=["probe_group"])
    report("delete visibility", len(q3) == 0, f"remaining={q3}")
except Exception as e:
    report("delete", False, str(e)[:120])

# upsert (re-insert same id with new vector should succeed / overwrite)
try:
    client.upsert(COL, data=[{"id": 2, "vector": rand_vec(), "probe_group": "a", "probe_state": "active"}])
    report("upsert", True)
except Exception as e:
    report("upsert", False, str(e)[:120])

# distance vs similarity: search returns distance; probe semantics
try:
    sr = client.search(COL, data=[rand_vec()], limit=1)
    # score field name is distance
    dist = sr[0][0].get("distance") if sr and sr[0] else None
    report("search returns distance field", dist is not None, f"distance={dist}")
    print(f"  note: raw score is distance (smaller = closer). score_semantics must map distance->similarity.")
except Exception as e:
    report("distance field", False, str(e)[:120])

# collection lifecycle
try:
    client.drop_collection(COL)
    report("drop_collection", not client.has_collection(COL))
except Exception as e:
    report("drop_collection", False, str(e)[:120])

print("\n=== smoke done ===")
# write manifest for CONDITIONS.md backfill
manifest = {"active_uri": ACTIVE_URI, "dim_probe": DIM, "metric_probe": ["COSINE","L2","IP"],
            "filter_probes": list(filters.keys()) + ["list_filter","delete","upsert"]}
open("/root/rivermind-data/kylin-smoke-env/smoke_manifest.json","w").write(json.dumps(manifest, indent=2))
