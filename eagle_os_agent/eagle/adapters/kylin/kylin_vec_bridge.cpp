// kylin_vec_bridge.cpp — C ABI bridge between Python (ctypes) and the real
// Kylin vector-engine C++ SDK (libkysdk-vector-engine-client.so.1).
//
// WHY THIS EXISTS
//   The installed SDK headers (/tmp/libkysdk-vector-engine-client/... or
//   /usr/include/kysdk-vector-engine-client) are STALE relative to the .so
//   ABI.  Reverse engineering (vtable + exported getters, see vtab*.py and
//   probes in /tmp) proved that:
//     * Database vtable order differs from Database.h  -> fixed in
//       kylin_db.h (used here), all calls past Disconnect previously
//       dispatched to the wrong slot.
//     * SearchArguments/QueryArguments member ORDER differs from the stale
//       headers, so the header's inline default ctors corrupt real members.
//       Real offsets recovered from exported getters:
//         SearchArguments: coll 0x00, partition_names 0x20, output_fields 0x50,
//                          expression 0x80, target_vectors(shared_ptr<Field>) 0xa0,
//                          extra_params(unordered_map) 0xf0, travel 0x128,
//                          guarantee 0x130, topk 0x138, round_decimal 0x140,
//                          radius 0x144, range_filter 0x148, range_search 0x14c,
//                          metric_type 0x150
//         QueryArguments : coll 0x00, partition_names 0x20, output_fields 0x50,
//                          expression 0x80, travel 0xa0, guarantee 0xa8,
//                          limit 0xb0, offset 0xb8
//   So we construct both argument objects ourselves with placement-new at the
//   real offsets instead of trusting any header ctor/setter.
//
// SCORE SEMANTICS
//   The engine returns COSINE SIMILARITY, best first (verified: identical
//   vector -> 1.0, orthogonal -> 0.0).  mem0's kylin vector store expects
//   row.score as COSINE DISTANCE (max(0, 1-sim)); the Python client converts.
//
// BUILD
//   g++ -std=c++17 -O2 -fPIC -shared \
//     -I/usr/include/kysdk-vector-engine-client \
//     -I<dir of kylin_db.h> \
//     kylin_vec_bridge.cpp -o libkylin_vec_bridge.so \
//     -Wl,-rpath,/usr/lib/x86_64-linux-gnu -ldl \
//     -l:libkysdk-vector-engine-client.so.1
//
// All entry points are extern "C" and return 0 on success (non-zero on
// failure; kvec_last_error() returns the message).  Output buffers are
// malloc'd and must be released with kvec_free / kvec_free_str_array.

#include "kylin_db.h"

#include <dlfcn.h>
#include <string.h>

#include <cstdint>
#include <iostream>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

using namespace VectorDB;
using nlohmann::json;

// ---------------------------------------------------------------------------
// real ABI-layout argument builders (must stay OUT of extern "C": templates
// cannot have C linkage)
// ---------------------------------------------------------------------------

namespace {

enum { SA_COLL = 0x00, SA_PART = 0x20, SA_OUTF = 0x50, SA_EXPR = 0x80,
       SA_TGT = 0xa0, SA_EPAR = 0xf0, SA_TRAV = 0x128, SA_GUAR = 0x130,
       SA_TOPK = 0x138, SA_RDEC = 0x140, SA_RAD = 0x144, SA_RFIL = 0x148,
       SA_RSCH = 0x14c, SA_METR = 0x150 };

struct RealSearchArgs {
    alignas(64) char buf[0x200];
    bool built = false;

    template <typename T>
    T* at(size_t off) { return reinterpret_cast<T*>(buf + off); }

    void build(const std::string& col, int topk, const std::string& expr,
               const std::vector<float>& query,
               const std::vector<std::string>& out_fields) {
        destroy();
        ::memset(buf, 0, sizeof(buf));
        ::new (buf + SA_COLL) std::string(col);
        ::new (buf + SA_PART) std::set<std::string>();
        ::new (buf + SA_OUTF) std::set<std::string>();
        for (const auto& f : out_fields) at<std::set<std::string>>(SA_OUTF)->insert(f);
        ::new (buf + SA_EXPR) std::string(expr);
        ::new (buf + SA_TGT) std::shared_ptr<Field>();
        *at<std::shared_ptr<Field>>(SA_TGT) =
            std::make_shared<FloatVecFieldData>("vector", std::vector<std::vector<float>>{query});
        ::new (buf + SA_EPAR) std::unordered_map<std::string, int64_t>();
        *(uint64_t*)(buf + SA_TRAV) = 0;
        *(uint64_t*)(buf + SA_GUAR) = 1;  // GuaranteeEventuallyTs() -> search immediately
        *(int64_t*)(buf + SA_TOPK) = topk < 1 ? 1 : topk;
        *(int32_t*)(buf + SA_RDEC) = -1;
        *(float*)(buf + SA_RAD) = 0.0f;
        *(float*)(buf + SA_RFIL) = 0.0f;
        *(bool*)(buf + SA_RSCH) = false;
        *(int32_t*)(buf + SA_METR) = static_cast<int32_t>(MetricType::COSINE);
        built = true;
    }

    void destroy() {
        if (!built) return;
        at<std::unordered_map<std::string, int64_t>>(SA_EPAR)->~unordered_map();
        at<std::shared_ptr<Field>>(SA_TGT)->~shared_ptr();
        at<std::string>(SA_EXPR)->~basic_string();
        at<std::set<std::string>>(SA_OUTF)->~set();
        at<std::set<std::string>>(SA_PART)->~set();
        at<std::string>(SA_COLL)->~basic_string();
        built = false;
    }

    ~RealSearchArgs() { destroy(); }
};

enum { QA_COLL = 0x00, QA_PART = 0x20, QA_OUTF = 0x50, QA_EXPR = 0x80,
       QA_TRAV = 0xa0, QA_GUAR = 0xa8, QA_LIMIT = 0xb0, QA_OFFSET = 0xb8 };

struct RealQueryArgs {
    alignas(64) char buf[0x200];
    bool built = false;

    template <typename T>
    T* at(size_t off) { return reinterpret_cast<T*>(buf + off); }

    void build(const std::string& col, const std::string& expr,
               const std::vector<std::string>& out_fields) {
        destroy();
        ::memset(buf, 0, sizeof(buf));
        ::new (buf + QA_COLL) std::string(col);
        ::new (buf + QA_PART) std::set<std::string>();
        ::new (buf + QA_OUTF) std::set<std::string>();
        for (const auto& f : out_fields) at<std::set<std::string>>(QA_OUTF)->insert(f);
        ::new (buf + QA_EXPR) std::string(expr);
        *(uint64_t*)(buf + QA_TRAV) = 0;
        *(uint64_t*)(buf + QA_GUAR) = 1;
        // The engine ignores limit/offset for expression queries (returns all
        // matches); Python truncates.  Keep sane values anyway.
        *(int64_t*)(buf + QA_LIMIT) = 16384;
        *(int64_t*)(buf + QA_OFFSET) = 0;
        built = true;
    }

    void destroy() {
        if (!built) return;
        at<std::string>(QA_EXPR)->~basic_string();
        at<std::set<std::string>>(QA_OUTF)->~set();
        at<std::set<std::string>>(QA_PART)->~set();
        at<std::string>(QA_COLL)->~basic_string();
        built = false;
    }

    ~RealQueryArgs() { destroy(); }
};

// Return a malloc'd (compact) JSON string, or nullptr on failure.
char* dup_json(const json& j) {
    const std::string s = j.dump();
    char* out = static_cast<char*>(malloc(s.size() + 1));
    if (!out) return nullptr;
    memcpy(out, s.c_str(), s.size() + 1);
    return out;
}

}  // namespace

extern "C" {

// ---------------------------------------------------------------------------
// errors
// ---------------------------------------------------------------------------

static thread_local std::string g_last_error;

const char* kvec_last_error(void) { return g_last_error.c_str(); }

static void set_err(const std::string& m) { g_last_error = m; }
static void set_err_status(const char* op, const Status& st) {
    std::string m = op;
    m += ": ";
    m += st.Message();
    set_err(m);
}

// ---------------------------------------------------------------------------
// handle / connection
// ---------------------------------------------------------------------------

struct KvecHandle {
    std::shared_ptr<Database> db;
    bool connected = false;
};

typedef void (*ConnectParamDefaultCtor)(void*);
typedef void (*ConnectParamUriCtor)(void*, const std::string*, unsigned short);

// The param is cached (the SDK does not keep per-connection state in it, and
// every call passes a const&) BUT it must track the requested UDS: a stale
// param built for a previous, different UDS would connect to the wrong socket.
// Rebuild only when the requested UDS changes; steady-state calls are free.
static ConnectParam* connect_param(const char* uds, bool* ok) {
    alignas(64) static char buf[16384];
    static std::string last_uds;
    *ok = false;
    const char* want = (uds != nullptr && *uds != '\0') ? uds : "/tmp/kylin-ai-vector-engine-0.sock";
    auto* cp = reinterpret_cast<ConnectParam*>(buf);
    if (!last_uds.empty() && last_uds == want) {
        *ok = true;
        return cp;
    }
    ::memset(buf, 0, sizeof(buf));
    if (strcmp(want, "/tmp/kylin-ai-vector-engine-0.sock") != 0) {
        auto ctor = (ConnectParamUriCtor)dlsym(
            RTLD_DEFAULT,
            "_ZN8VectorDB12ConnectParamC1ENSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEt");
        if (!ctor) {
            set_err("ConnectParam(uri, port) ctor not exported; cannot honour custom KYLIN_VECTOR_UDS");
            return cp;
        }
        std::string uri = std::string("unix:") + want;
        ctor(buf, &uri, (unsigned short)0);
    } else {
        auto ctor = (ConnectParamDefaultCtor)dlsym(RTLD_DEFAULT, "_ZN8VectorDB12ConnectParamC1Ev");
        if (!ctor) {
            set_err("dlsym ConnectParam default ctor failed");
            return cp;
        }
        ctor(buf);
    }
    last_uds = want;
    *ok = true;
    return cp;
}

KvecHandle* kvec_connect(const char* uds, int* err_code, char** err_msg) {
    *err_code = 0;
    if (err_msg) *err_msg = nullptr;
    bool ok = false;
    ConnectParam* cp = connect_param(uds, &ok);
    if (!ok) {
        *err_code = 1;
        if (err_msg) *err_msg = strdup(g_last_error.c_str());
        return nullptr;
    }
    auto* h = new KvecHandle();
    h->db = Database::Create();
    if (!h->db) {
        set_err("Database::Create returned null");
        *err_code = 2;
        if (err_msg) *err_msg = strdup(g_last_error.c_str());
        delete h;
        return nullptr;
    }
    Status st = h->db->Connect(*cp);
    if (!st.IsOk()) {
        set_err_status("Connect", st);
        *err_code = 3;
        if (err_msg) *err_msg = strdup(g_last_error.c_str());
        delete h;
        return nullptr;
    }
    h->connected = true;
    return h;
}

void kvec_destroy(KvecHandle* h) {
    if (h && h->db) h->db->Disconnect();
    delete h;
}

// ---------------------------------------------------------------------------
// collections
// ---------------------------------------------------------------------------

int kvec_has_collection(KvecHandle* h, const char* name, int* out_has) {
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }
    if (out_has) *out_has = 0;
    bool has = false;
    Status st = h->db->HasCollection(std::string(name), has);
    if (!st.IsOk()) { set_err_status("HasCollection", st); return 1; }
    if (out_has) *out_has = has ? 1 : 0;
    return 0;
}

int kvec_ensure_collection(KvecHandle* h, const char* name, int dim, const char* metric) {
    (void)metric;  // always cosine in this deployment
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }
    std::string col(name);
    bool has = false;
    Status st = h->db->HasCollection(col, has);
    if (!st.IsOk()) { set_err_status("HasCollection", st); return 1; }
    if (has) return 0;
    st = h->db->CreateCollection(col, dim, /*auto_id=*/false, /*enable_dynamic_field=*/true);
    if (!st.IsOk()) { set_err_status("CreateCollection", st); return 1; }
    return 0;
}

int kvec_drop_collection(KvecHandle* h, const char* name) {
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }
    Status st = h->db->DropCollection(std::string(name));
    if (!st.IsOk()) { set_err_status("DropCollection", st); return 1; }
    return 0;
}

// ---------------------------------------------------------------------------
// upsert
// ---------------------------------------------------------------------------

// ids: n int64 primary keys; vectors: n*dim floats row-major;
// payloads: n compact-JSON strings (parsed into the "$meta" dynamic field).
int kvec_upsert(KvecHandle* h, const char* collection,
                const int64_t* ids, int n,
                const float* vectors, int dim,
                const char** payloads, int n_payloads) {
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }
    if (n <= 0) return 0;
    std::vector<int64_t> id_vec(ids, ids + n);
    std::vector<std::vector<float>> float_vec(n, std::vector<float>(dim));
    for (int i = 0; i < n; ++i)
        memcpy(float_vec[i].data(), vectors + (size_t)i * dim, sizeof(float) * dim);
    std::vector<json> payload_json;
    payload_json.reserve(n_payloads);
    for (int i = 0; i < n_payloads; ++i) {
        try {
            payload_json.push_back(json::parse(payloads[i]));
        } catch (...) {
            set_err("payload " + std::to_string(i) + " is not valid JSON");
            return 1;
        }
    }
    std::vector<FieldDataPtr> fields;
    fields.push_back(std::make_shared<Int64FieldData>("id", id_vec));
    fields.push_back(std::make_shared<FloatVecFieldData>("vector", float_vec));
    fields.push_back(std::make_shared<JsonFieldData>("$meta", payload_json));
    DmlResults dml;
    Status st = h->db->Upsert(std::string(collection), fields, dml);
    if (!st.IsOk()) { set_err_status("Upsert", st); return 1; }
    return 0;
}

// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------

// Single query vector.  Returns aligned hit arrays (top-k best first):
//   out_n ids scores payloads[json strings]; vectors only when want_vector.
// Caller frees with kvec_free (arrays) / kvec_free_str_array (strings).
int kvec_search(KvecHandle* h, const char* collection,
                const float* query, int dim, int limit, const char* expr,
                int* out_n, int64_t** out_ids, float** out_scores,
                char*** out_payloads, char*** out_vectors, int want_vector) {
    if (out_n) *out_n = 0;
    if (out_ids) *out_ids = nullptr;
    if (out_scores) *out_scores = nullptr;
    if (out_payloads) *out_payloads = nullptr;
    if (out_vectors) *out_vectors = nullptr;
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }

    std::vector<std::string> outf = {"$meta"};
    if (want_vector) outf.push_back("vector");
    std::vector<float> q(query, query + dim);
    RealSearchArgs sa;
    sa.build(std::string(collection), limit, expr ? expr : "", q, outf);

    SearchResults sr;
    Status st = h->db->Search(*reinterpret_cast<SearchArguments*>(sa.buf), sr, 5000);
    if (!st.IsOk()) { set_err_status("Search", st); return 1; }

    if (sr.Results().empty()) return 0;
    auto& res = sr.Results()[0];
    const auto& ids = res.Ids().IntIDArray();
    const auto& scores = res.Scores();
    size_t n = ids.size();
    if (n == 0) return 0;

    // locate output fields
    const std::vector<json>* payloads_arr = nullptr;
    const std::vector<std::vector<float>>* vec_arr = nullptr;
    for (const auto& f : res.OutputFields()) {
        auto* jf = dynamic_cast<JsonFieldData*>(f.get());
        if (jf) { payloads_arr = &jf->Data(); continue; }
        auto* vf = dynamic_cast<FloatVecFieldData*>(f.get());
        if (vf) vec_arr = &vf->Data();
    }

    auto* ids_out = static_cast<int64_t*>(malloc(n * sizeof(int64_t)));
    auto* scores_out = static_cast<float*>(malloc(n * sizeof(float)));
    auto** payloads_out = static_cast<char**>(calloc(n, sizeof(char*)));
    auto** vectors_out = want_vector ? static_cast<char**>(calloc(n, sizeof(char*))) : nullptr;
    if (!ids_out || !scores_out || !payloads_out || (want_vector && !vectors_out)) {
        free(ids_out); free(scores_out); free(payloads_out); free(vectors_out);
        set_err("kvec_search: out of memory");
        return 1;
    }
    for (size_t i = 0; i < n; ++i) {
        ids_out[i] = ids[i];
        scores_out[i] = scores[i];
        json p = (payloads_arr && i < payloads_arr->size()) ? (*payloads_arr)[i] : json::object();
        payloads_out[i] = dup_json(p);
        if (want_vector && vec_arr && i < vec_arr->size()) {
            vectors_out[i] = dup_json(json((*vec_arr)[i]));
        } else if (want_vector) {
            vectors_out[i] = dup_json(json(std::vector<float>{}));
        }
    }
    if (out_n) *out_n = (int)n;
    if (out_ids) *out_ids = ids_out; else free(ids_out);
    if (out_scores) *out_scores = scores_out; else free(scores_out);
    if (out_payloads) *out_payloads = payloads_out; else {
        for (size_t i = 0; i < n; ++i) free(payloads_out[i]);
        free(payloads_out);
    }
    if (out_vectors) *out_vectors = vectors_out;
    return 0;
}

// ---------------------------------------------------------------------------
// query (get by expr / list)
// ---------------------------------------------------------------------------

int kvec_query(KvecHandle* h, const char* collection, const char* expr, int want_vector,
               int* out_n, int64_t** out_ids, char*** out_payloads, char*** out_vectors) {
    if (out_n) *out_n = 0;
    if (out_ids) *out_ids = nullptr;
    if (out_payloads) *out_payloads = nullptr;
    if (out_vectors) *out_vectors = nullptr;
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }

    std::vector<std::string> outf = {"$meta"};
    if (want_vector) outf.push_back("vector");
    RealQueryArgs qa;
    qa.build(std::string(collection), expr ? expr : "", outf);

    QueryResults qr;
    Status st = h->db->Query(*reinterpret_cast<QueryArguments*>(qa.buf), qr, 5000);
    if (!st.IsOk()) { set_err_status("Query", st); return 1; }

    const std::vector<int64_t>* ids_arr = nullptr;
    const std::vector<json>* payloads_arr = nullptr;
    const std::vector<std::vector<float>>* vec_arr = nullptr;
    for (const auto& f : qr.OutputFields()) {
        auto* i64 = dynamic_cast<Int64FieldData*>(f.get());
        if (i64) { ids_arr = &i64->Data(); continue; }
        auto* jf = dynamic_cast<JsonFieldData*>(f.get());
        if (jf) { payloads_arr = &jf->Data(); continue; }
        auto* vf = dynamic_cast<FloatVecFieldData*>(f.get());
        if (vf) vec_arr = &vf->Data();
    }
    size_t n = ids_arr ? ids_arr->size() : (payloads_arr ? payloads_arr->size() : 0);
    if (n == 0) return 0;

    auto* ids_out = ids_arr ? static_cast<int64_t*>(malloc(n * sizeof(int64_t))) : nullptr;
    if (ids_arr) for (size_t i = 0; i < n; ++i) ids_out[i] = (*ids_arr)[i];
    auto** payloads_out = static_cast<char**>(calloc(n, sizeof(char*)));
    auto** vectors_out = want_vector ? static_cast<char**>(calloc(n, sizeof(char*))) : nullptr;
    if ((ids_arr && !ids_out) || !payloads_out || (want_vector && !vectors_out)) {
        free(ids_out); free(payloads_out); free(vectors_out);
        set_err("kvec_query: out of memory");
        return 1;
    }
    for (size_t i = 0; i < n; ++i) {
        json p = (payloads_arr && i < payloads_arr->size()) ? (*payloads_arr)[i] : json::object();
        payloads_out[i] = dup_json(p);
        if (want_vector && vec_arr && i < vec_arr->size())
            vectors_out[i] = dup_json(json((*vec_arr)[i]));
        else if (want_vector)
            vectors_out[i] = dup_json(json(std::vector<float>{}));
    }
    if (out_n) *out_n = (int)n;
    if (out_ids) *out_ids = ids_out; else free(ids_out);
    if (out_payloads) *out_payloads = payloads_out; else {
        for (size_t i = 0; i < n; ++i) free(payloads_out[i]);
        free(payloads_out);
    }
    if (out_vectors) *out_vectors = vectors_out;
    return 0;
}

// ---------------------------------------------------------------------------
// delete
// ---------------------------------------------------------------------------

int kvec_delete_expr(KvecHandle* h, const char* collection, const char* expr) {
    if (!h || !h->connected || !h->db) { set_err("not connected"); return 1; }
    DmlResults dml;
    Status st = h->db->Delete(std::string(collection), expr ? expr : "", dml);
    if (!st.IsOk()) { set_err_status("Delete", st); return 1; }
    return 0;
}

// ---------------------------------------------------------------------------
// free helpers
// ---------------------------------------------------------------------------

void kvec_free(void* p) { free(p); }

void kvec_free_str_array(char** arr, int n) {
    if (!arr) return;
    for (int i = 0; i < n; ++i) free(arr[i]);
    free(arr);
}

}  // extern "C"
