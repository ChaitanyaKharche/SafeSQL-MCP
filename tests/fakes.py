"""Test doubles for the Messages API."""

from types import SimpleNamespace


class ScriptedLLM:
    """Stands in for the Messages API: returns pre-written assistant turns in order."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # the agent appends in place
        blocks = self.turns.pop(0)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        usage = SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, model="claude-haiku-5-5")


def tool_use(i, name, **inp):
    return SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=inp)
