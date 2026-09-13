"""Real Kylin vector adapter — talks to the live ``kylin-ai-vector-engine``
daemon over its unix socket through the C ABI bridge
(``libkylin_vec_bridge.so``, see ``kylin_vec_bridge.cpp``).

The installed C++ SDK headers are stale relative to the shipped
``libkysdk-vector-engine-client.so.1`` ABI, so the bridge constructs
``SearchArguments``/``QueryArguments`` at reverse-engineered real offsets.
Everything here is deliberately simple: it marshals ctypes buffers to the
bridge and translates the ``kylin-normalized-v1`` filter dialect.

Semantics (verified empirically against the engine):
  * engine Search returns COSINE SIMILARITY, best first -> we expose
    ``row.score`` as COSINE DISTANCE (max(0, 1 - sim)) because mem0's kylin
    vector store maps ``score_semantics="cosine_distance"`` via
    ``similarity = max(0, 1 - score)``.
  * primary keys are int64; mem0 ids are UUID strings, so we map each string
    to a stable 63-bit hash and store the original in the payload as
    ``__id__`` (mirrors the shim's convention).
  * supported expression operators: ``== != in not in &&/||`` (``not (...)``
    is rejected by the engine, so ``$not`` is rewritten via De Morgan).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path

from eagle.adapters.kylin.vector_client import KylinVectorRow

_BRIDGE_PATH = Path(__file__).resolve().parent / "libkylin_vec_bridge.so"

_c_int = ctypes.c_int
_c_int64 = ctypes.c_int64
_c_float = ctypes.c_float
_c_char_p = ctypes.c_char_p
_c_void_p = ctypes.c_void_p
_p = ctypes.POINTER


def _load_bridge(path: str | os.PathLike | None = None):
    lib = ctypes.CDLL(str(path or _BRIDGE_PATH))
    lib.kvec_connect.argtypes = [_c_char_p, _p(_c_int), _p(_c_char_p)]
    lib.kvec_connect.restype = _c_void_p
    lib.kvec_destroy.argtypes = [_c_void_p]
    lib.kvec_destroy.restype = None
    lib.kvec_last_error.argtypes = []
    lib.kvec_last_error.restype = _c_char_p
    for fn, args in {
        "kvec_has_collection": [_c_void_p, _c_char_p, _p(_c_int)],
        "kvec_ensure_collection": [_c_void_p, _c_char_p, _c_int, _c_char_p],
        "kvec_drop_collection": [_c_void_p, _c_char_p],
        "kvec_delete_expr": [_c_void_p, _c_char_p, _c_char_p],
    }.items():
        f = getattr(lib, fn)
        f.argtypes = args
        f.restype = _c_int
    lib.kvec_upsert.argtypes = [
        _c_void_p, _c_char_p,
        _p(_c_int64), _c_int,          # ids, n
        _p(_c_float), _c_int,          # vectors (n*dim row-major), dim
        _p(_c_char_p), _c_int,         # payload JSON strings, n
    ]
    lib.kvec_upsert.restype = _c_int
    lib.kvec_search.argtypes = [
        _c_void_p, _c_char_p,          # handle, collection
        _p(_c_float), _c_int, _c_int, _c_char_p,  # query, dim, limit, expr
        _p(_c_int), _p(_p(_c_int64)), _p(_p(_c_float)),  # n, ids, scores
        _p(_p(_c_char_p)), _p(_p(_c_char_p)), _c_int,    # payloads, vectors, want_vector
    ]
    lib.kvec_search.restype = _c_int
    lib.kvec_query.argtypes = [
        _c_void_p, _c_char_p, _c_char_p, _c_int,        # handle, collection, expr, want_vector
        _p(_c_int), _p(_p(_c_int64)), _p(_p(_c_char_p)), _p(_p(_c_char_p)),  # n, ids, payloads, vectors
    ]
    lib.kvec_query.restype = _c_int
    lib.kvec_free.argtypes = [_c_void_p]
    lib.kvec_free.restype = None
    lib.kvec_free_str_array.argtypes = [_p(_c_char_p), _c_int]
    lib.kvec_free_str_array.restype = None
    return lib


def _stable_pk(text: str) -> int:
    """Stable, non-negative 63-bit primary key for an arbitrary id string."""
    raw = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(raw, "big") & 0x7FFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# filter-dialect translation  (kylin-normalized-v1 -> engine expression)
# ---------------------------------------------------------------------------

def _val(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _vals(values):
    return ", ".join(_val(v) for v in values)


def _ids(ids):
    return ", ".join(str(_stable_pk(str(i))) for i in ids)


def _negate(condition: dict) -> str:
    """Return an expression for NOT(condition).  Engine rejects ``not (...)``
    and ``!(...)``, so negate each leaf operator instead (De Morgan)."""
    parts = []
    for key, spec in condition.items():
        if key == "$not":
            # NOT(NOT(cs)) with our "none match" reading = cs ORed back together
            inner = [_negate(c) if c else "" for c in spec]
            parts.append("(" + " || ".join(x for x in inner if x) + ")")
        elif key == "id":
            if isinstance(spec, dict):
                if "in" in spec:
                    parts.append(f"id not in [{_ids(spec['in'])}]")
                elif "nin" in spec:
                    parts.append(f"id in [{_ids(spec['nin'])}]")
                elif "ne" in spec:
                    parts.append(f"id == {_stable_pk(str(spec['ne']))}")
                else:
                    parts.append(f"id != {_stable_pk(str(spec))}")
            else:
                parts.append(f"id != {_stable_pk(str(spec))}")
        elif isinstance(spec, dict):
            if "in" in spec:
                parts.append(f"{key} not in [{_vals(spec['in'])}]")
            elif "nin" in spec:
                parts.append(f"{key} in [{_vals(spec['nin'])}]")
            elif "ne" in spec:
                parts.append(f"{key} == {_val(spec['ne'])}")
            else:
                parts.append(f"{key} != {_val(spec)}")
        else:
            parts.append(f"{key} != {_val(spec)}")
    return " && ".join(parts) if len(parts) > 1 else parts[0]


def _translate_filters(filters: dict) -> str:
    """kylin-normalized-v1 filter dict -> engine boolean expression ('' if none)."""
    if not filters:
        return ""
    parts = []
    for key, spec in filters.items():
        if key == "$not":
            # $not semantics: row must match NONE of the sub-conditions
            # -> NOT(c1 || c2 || ...) == NOT c1 && NOT c2 && ...
            negs = [_negate(c) for c in spec]
            parts.append("(" + " && ".join(negs) + ")")
        elif key == "id":
            if isinstance(spec, dict):
                if "in" in spec:
                    parts.append(f"id in [{_ids(spec['in'])}]")
                elif "nin" in spec:
                    parts.append(f"id not in [{_ids(spec['nin'])}]")
                elif "ne" in spec:
                    parts.append(f"id != {_stable_pk(str(spec['ne']))}")
                else:
                    parts.append(f"id == {_stable_pk(str(spec))}")
            else:
                parts.append(f"id == {_stable_pk(str(spec))}")
        elif isinstance(spec, dict):
            if "in" in spec:
                parts.append(f"{key} in [{_vals(spec['in'])}]")
            elif "nin" in spec:
                parts.append(f"{key} not in [{_vals(spec['nin'])}]")
            elif "ne" in spec:
                parts.append(f"{key} != {_val(spec['ne'])}")
            else:
                parts.append(f"{key} == {_val(spec)}")
        else:
            parts.append(f"{key} == {_val(spec)}")
    return " && ".join(parts)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class RealVectorClient:
    """Drops into the KylinVectorClient Protocol, backed by the real engine."""

    backend = "real"

    def __init__(self, uds: str | None = None, bridge_path: str | os.PathLike | None = None):
        self._lib = _load_bridge(bridge_path)
        self._uds = uds or os.getenv("KYLIN_VECTOR_UDS", "") or "/tmp/kylin-ai-vector-engine-0.sock"
        self._collections: dict[str, dict] = {}
        self._h: int | None = None
        self.connected = False
        self._connect()

    # -- plumbing ----------------------------------------------------------

    def _connect(self):
        err_code = _c_int(0)
        err_msg = _c_char_p()
        h = self._lib.kvec_connect(self._uds.encode(), ctypes.byref(err_code), ctypes.byref(err_msg))
        if not h:
            detail = err_msg.value.decode() if err_msg.value else "unknown error"
            raise RuntimeError(f"kylin vector engine connect failed ({err_code.value}): {detail}")
        self._h = int(h)
        self.connected = True

    def _err(self) -> str:
        raw = self._lib.kvec_last_error()
        return raw.decode() if raw else "unknown bridge error"

    def _check(self, rc: int, op: str):
        if rc:
            raise RuntimeError(f"{op} failed: {self._err()}")

    def _has(self, name: str) -> bool:
        out = _c_int(0)
        rc = self._lib.kvec_has_collection(self._h, name.encode(), ctypes.byref(out))
        self._check(rc, "has_collection")
        return bool(out.value)

    def __del__(self):  # pragma: no cover - best effort
        try:
            if getattr(self, "_h", None):
                self._lib.kvec_destroy(self._h)
                self._h = None
        except Exception:
            pass

    # -- protocol ----------------------------------------------------------

    def ensure_collection(self, *, name: str, dimension: int, metric: str) -> None:
        rc = self._lib.kvec_ensure_collection(
            self._h, name.encode(), int(dimension), metric.encode()
        )
        self._check(rc, "ensure_collection")
        self._collections[name] = {"dimension": int(dimension), "metric": metric}

    def list_collections(self):
        return list(self._collections.keys())

    def collection_info(self, *, name: str):
        return self._collections.get(name)

    def delete_collection(self, *, name: str) -> None:
        if name in self._collections:
            rc = self._lib.kvec_drop_collection(self._h, name.encode())
            self._check(rc, "delete_collection")
            self._collections.pop(name, None)

    def healthcheck(self, *, collection: str) -> bool:
        if not self.connected:
            return False
        return self._has(collection)

    def upsert(self, *, collection: str, vectors, payloads, ids) -> None:
        n = len(ids)
        if n == 0:
            return
        first = vectors[0]
        vecs = list(vectors) if isinstance(first, (list, tuple)) else [list(vectors)]
        if len(vecs) < n:
            vecs = vecs * n  # broadcast a single vector
        dim = len(vecs[0])
        flat = [float(v) for vec in vecs for v in vec]
        id_arr = (_c_int64 * n)(*(_stable_pk(str(i)) for i in ids))
        vec_arr = (_c_float * (n * dim))(*flat)
        payloads = [
            json.dumps(dict(p) | {"__id__": str(i)}, ensure_ascii=False).encode()
            for p, i in zip(payloads, ids)
        ]
        pay_arr = (_c_char_p * n)(*payloads)
        rc = self._lib.kvec_upsert(
            self._h, collection.encode(),
            id_arr, n, vec_arr, dim, pay_arr, n,
        )
        self._check(rc, "upsert")

    def search(self, *, collection: str, vector, limit: int, filters: dict) -> list[KylinVectorRow]:
        vec = list(vector[0]) if isinstance(vector[0], (list, tuple)) else list(vector)
        dim = len(vec)
        q = (_c_float * dim)(*vec)
        expr = _translate_filters(filters or {})
        n = _c_int(0)
        ids_p = _p(_c_int64)()
        scores_p = _p(_c_float)()
        pays_p = _p(_c_char_p)()
        vecs_p = _p(_c_char_p)()
        rc = self._lib.kvec_search(
            self._h, collection.encode(), q, dim, max(1, int(limit)),
            expr.encode() if expr else None,
            ctypes.byref(n), ctypes.byref(ids_p), ctypes.byref(scores_p),
            ctypes.byref(pays_p), ctypes.byref(vecs_p), 0,
        )
        self._check(rc, "search")
        count = n.value
        try:
            rows = []
            for i in range(count):
                payload = json.loads(pays_p[i].decode()) if pays_p[i] else {}
                row_id = str(payload.get("__id__") or ids_p[i])
                sim = float(scores_p[i])
                rows.append(
                    KylinVectorRow(id=row_id, score=max(0.0, 1.0 - sim), payload=payload)
                )
            return rows
        finally:
            self._lib.kvec_free(ids_p)
            self._lib.kvec_free(scores_p)
            self._lib.kvec_free_str_array(pays_p, count)
            self._lib.kvec_free_str_array(vecs_p, count)

    def delete(self, *, collection: str, vector_id: str) -> None:
        expr = f"id == {_stable_pk(str(vector_id))}"
        rc = self._lib.kvec_delete_expr(self._h, collection.encode(), expr.encode())
        self._check(rc, "delete")

    def get(self, *, collection: str, vector_id: str, include_vector: bool) -> KylinVectorRow | None:
        expr = f"id == {_stable_pk(str(vector_id))}"
        rows = self._query_rows(collection, expr, want_vector=include_vector)
        for row in rows:
            if row.id == vector_id or _stable_pk(row.id) == _stable_pk(str(vector_id)):
                return row
        return None

    def list(self, *, collection: str, filters: dict, limit: int | None) -> list[KylinVectorRow]:
        expr = _translate_filters(filters or {})
        if not expr:
            expr = "id >= 0"  # always-true: engine requires a non-empty expression
        rows = self._query_rows(collection, expr, want_vector=False)
        if limit is not None:
            rows = rows[:limit]
        return rows

    # -- internals ---------------------------------------------------------

    def _query_rows(self, collection: str, expr: str, *, want_vector: bool) -> list[KylinVectorRow]:
        n = _c_int(0)
        ids_p = _p(_c_int64)()
        pays_p = _p(_c_char_p)()
        vecs_p = _p(_c_char_p)()
        rc = self._lib.kvec_query(
            self._h, collection.encode(), expr.encode(), int(want_vector),
            ctypes.byref(n), ctypes.byref(ids_p), ctypes.byref(pays_p), ctypes.byref(vecs_p),
        )
        self._check(rc, "query")
        count = n.value
        try:
            rows = []
            for i in range(count):
                payload = json.loads(pays_p[i].decode()) if pays_p[i] else {}
                vector = None
                if want_vector and vecs_p[i]:
                    vector = [float(x) for x in json.loads(vecs_p[i].decode())]
                row_id = str(payload.get("__id__") or ids_p[i])
                rows.append(KylinVectorRow(id=row_id, score=0.0, payload=payload, vector=vector))
            return rows
        finally:
            self._lib.kvec_free(ids_p)
            self._lib.kvec_free_str_array(pays_p, count)
            self._lib.kvec_free_str_array(vecs_p, count)


def make_vector_client():
    """Env-driven factory honouring KYLIN_USE_SHIM (default 1 = shim).

    KYLIN_USE_SHIM=0 -> try the real engine through the bridge; on any
    failure (daemon down, bridge missing) fall back to the shim and report
    ``backend`` honestly so harnesses never mislabel a run as real.
    """
    if os.getenv("KYLIN_USE_SHIM", "1") != "0":
        from eagle.adapters.kylin.vector_shim import ShimVectorClient

        c = ShimVectorClient()
        c.backend = "shim"  # type: ignore[attr-defined]
        return c
    try:
        c = RealVectorClient()
        c.backend = "real"
        return c
    except Exception as exc:  # noqa: BLE001 - external SDK boundary
        from eagle.adapters.kylin.vector_shim import ShimVectorClient

        c = ShimVectorClient()
        c.backend = f"shim(fallback: {type(exc).__name__})"  # type: ignore[attr-defined]
        return c
