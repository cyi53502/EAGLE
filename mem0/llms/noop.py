from mem0.llms.base import LLMBase


class NoopLLM(LLMBase):
    def generate_response(self, messages, tools=None, tool_choice="auto", **kwargs):
        raise RuntimeError(
            "NoopLLM was invoked. EAGLE persistence must call Memory.add with infer=False."
        )
