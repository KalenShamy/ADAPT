"""Test doubles shared by several test files."""

import json


class FakeClient:
    """An LLM client that answers every prompt with the same JSON value."""

    name = "fake"
    model = "fake"

    def __init__(self, answer):
        self.answer = answer

    def complete(self, prompt):
        return json.dumps(self.answer)
