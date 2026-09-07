import importlib
import os
import sys


def test_bootstrap_disables_mem0_telemetry_before_memory_import(monkeypatch):
    monkeypatch.delenv("MEM0_TELEMETRY", raising=False)
    sys.modules.pop("eagle.bootstrap", None)

    importlib.import_module("eagle.bootstrap")

    assert os.environ["MEM0_TELEMETRY"] == "False"
