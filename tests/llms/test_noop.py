import pytest

from mem0.configs.llms.base import BaseLlmConfig
from mem0.llms.noop import NoopLLM


def test_noop_llm_fails_if_inference_is_invoked():
    llm = NoopLLM(BaseLlmConfig())

    with pytest.raises(RuntimeError, match="infer=False"):
        llm.generate_response([])
