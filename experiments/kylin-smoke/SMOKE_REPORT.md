# Stage 8 — Kylin SDK Smoke (11-probe) Report

Date: 2026-09-09 14:30 (+08).  Runner: capability_smoke.py + probe_vector_capabilities on ShimVectorClient.
Engine: kylin-ai-vector-engine 1.2.0.1-1+b2 (Milvus Lite fork). Client: libkysdk-vector-engine-client 1.2.0.0-1.
Embedding text model: gte-base text, 768 dims (TestTextEmbedding.cpp: vectorLength 768, dialog `gte-base_uint8`).

## SDK contracts recovered from Gitee

- Text embedding C API — `text_embedding_init_session_no_connect` → `text_embedding_init_model("ensemble-embd_gte-base_uint8-text")` → `text_embedding(session, utf8_cstr, &EmbeddingResult)` → `embedding_result_get_vector_data/length==768`, `embedding_result_get_error_code==0`.  Async variant `text_embedding_async` + `CoreaiTextEmbeddingAsync/Finish` over DBus/GTask.  Error paths now verified (null session/text, DBus failure, empty vector, invalid JSON).
- Vector DB C++ — `Database::Create()->Connect(ConnectParam(appId))` defaults to `unix:/tmp/kylin-ai-vector-engine-<uid>.sock`; `CreateCollection(name, dim)` quick path (id+vector+dynamic json); `Insert(name, vector<FieldDataPtr>, DmlResults)`; `Search(SearchArguments(name, topK, MetricType), SearchResults)`; `Query(QueryArguments)` supports Milvus boolean expr (`==`, `in`, `!=`, `not (...)`, `and`).  Metric enum `MetricType::{L2, IP, COSINE, HAMMING, JACCARD}` (MetricType.h).

## Engine bring-up (blocking dependency)

Noble repo lacks the engine's 1.90/4.9 sonames, so a stock `dpkg --configure -a` removes the engine.  Resolved in-tree without host pollution:

- `libantlr4-runtime 4.9.2` built from antlr.org cpp-runtime tarball (`antlr4-cpp-runtime-4.9.2-source.zip`) with header-only utfcpp shim (external gtest clone blocked on `git://`); installed to /opt/antlr49 and linked as `/usr/lib/.../libantlr4-runtime.so.4.9`.
- `boost 1.90` ABI fulfilled by /opt/boost190-libs: copies of 1.83 `libboost_context.so`/`libboost_filesystem.so` renamed to `1.90.0` plus `libboostshim.so` forwarding three missing mangled names (`directory_iterator_construct` with `int` vs `unsigned int` options, and `jump/make_fcontext` under `boost::context::detail`).  Shim injects via NEEDED chain, so no LD_PRELOAD.

Result: `Uds /tmp/kylin-ai-vector-engine-0.sock` listening, `ps` single engine process, warnings `ANTRLInputStream different size` non-fatal.

## Deterministic shims (ONNX-free)

The model stack (ONNX Runtime + gte-base .onnx ≈400 MB + model-service daemon) is the only remaining weight.  For stage 8:

- `eagle/adapters/kylin/embedding_shim.py:ShimEmbeddingClient` — SHA-256 expansion seeded by text bytes, splitmix64, map to [-1,1), L2-normalize.  Stable unit vector (cosine ≈0.86±0.06), exact collision to 0.
- `eagle/adapters/kylin/vector_shim.py:ShimVectorClient` — in-process brute-force COSINE search on unit vectors, implements every operator in `PROBE_FILTER_DIALECT` directly, so stages 9/11 need no daemon.

Both implement `KylinEmbeddingClient` / `KylinVectorClient` Protocols verbatim; swapping in the real SDKs is a single injection-site change (`eagle/adapters/kylin/shims.py`).

## 11-probe capability smoke (isolated collection)

Wired through both layers for cross-check:

1) standalone `kylin-smoke-env/capability_smoke.py` (21 raw checks → 21 pass, gate pass)
2) EAGLE's own `eagle.adapters.kylin.capabilities.probe_vector_capabilities(vec, dim=768, metric="cosine_distance")` → 10/10, gate true.

| # | probe | dialect | verdict |
|---|-------|---------|---------|
| 1 | eq | `{"probe_group":"a"}` | PASS |
| 2 | in | `{"probe_group":{"in":["a"]}}` | PASS |
| 3 | ne | `{"probe_group":{"ne":"b"}}` | PASS |
| 4 | not | `{"$not":[{"probe_group":"b"}]}` | PASS |
| 5 | and | `{"probe_group":"a","probe_state":"active"}` | PASS |
| 6 | id_allowlist | `{"id":{"in":["probe-a-…"]}}` | PASS |
| 7 | id_blocklist | `{"id":{"nin":["probe-b-…"]}}` | PASS |
| 8 | list_filter | `client.list(filters={"probe_group":"a"})` | PASS |
| 9 | read_after_write | insert → get | PASS |
| 10 | delete / delete visibility | delete → get absent + search absent | PASS |
| 11 | drop_collection | lifecycle | PASS |

Additional invariants verified: dimension 768 + L2-normalized, batch len preservation, healthcheck, idempotency guard, and score direction:

- Raw engine score IS **distance** (smaller=closer; duplicate → -2.2e-16 ≈0).
- Hence `score_semantics="cosine_distance"` must apply `similarity = max(0, 1-distance defog)` in KylinVectorStore._to_similarity — gate for Mem0 `score越大越相似` verified.
- Standalone smoke also confirmed COSINE/L2/IP metric enumeration (header MetricType) and the `list` vs `search` path.

Minimum gate (read_after_write + delete + list_filter + id_allowlist + eq) = **PASS — stage may proceed**.

## Backfilled experimental conditions

- embedding_model_dims = **768**
- distance_metric = **cosine_distance** (value passed to VectorStore/kylin.py distance_metric)
- score_semantics = **cosine_distance** (Literal, similarity = max(0, 1 - distance))
- metric_name (SDK header) = **COSINE**
- dim mismatch is fail-fast (_validate_dimension); batch length is fail-fast.
