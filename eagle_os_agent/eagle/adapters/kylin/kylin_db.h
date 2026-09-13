// Self-authored Database interface matching the REAL vtable layout of
// libkysdk-vector-engine-client.so.1 (MilvusClientImpl). The on-disk
// Database.h is stale (extra virtuals), which shifts every slot after
// Connect/Disconnect by one and corrupts calls.
#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include "types/Status.h"
#include "types/ConnectParam.h"
#include "types/DmlResults.h"
#include "types/SearchArguments.h"
#include "types/SearchResults.h"
#include "types/QueryArguments.h"
#include "types/QueryResults.h"
#include "types/FieldData.h"
#include "types/CollectionSchema.h"
#include "types/IndexDesc.h"

namespace VectorDB {

class Database {
public:
    // real primary-vtable order (verified from .rela.dyn of the .so)
    virtual Status Connect(const ConnectParam& connect_param) = 0;
    virtual Status Disconnect() = 0;
    virtual Status CreateCollection(const CollectionSchema& schema, const IndexDesc& index_desc) = 0;
    virtual Status CreateCollection(const std::string& collection_name, int dim, bool auto_id = true,
                                    bool enable_dynamic_field = true) = 0;
    virtual Status HasCollection(const std::string& collection_name, bool& has) = 0;
    virtual Status DropCollection(const std::string& collection_name) = 0;
    virtual Status Insert(const std::string& collection_name, const std::vector<FieldDataPtr>& fields,
                          DmlResults& results) = 0;
    virtual Status Delete(const std::string& collection_name, const std::string& expression,
                          DmlResults& results) = 0;
    virtual Status Upsert(const std::string& collection_name, const std::vector<FieldDataPtr>& fields,
                          DmlResults& results) = 0;
    virtual Status Search(const SearchArguments& arguments, SearchResults& results, int timeout = 0) = 0;
    virtual Status Query(const QueryArguments& arguments, QueryResults& results, int timeout = 0) = 0;
    virtual Status LoadCollection(const std::string& collection_name) = 0;
    virtual Status ReleaseCollection(const std::string& collection_name) = 0;
    virtual ~Database() = default;

    static std::shared_ptr<Database> Create();
};

}  // namespace VectorDB
