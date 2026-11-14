"""LLM backends for the generate stage. fill.py only depends on LlmClient."""

import json
import os
import subprocess
from typing import Protocol

DEFAULT_MODEL = "sonnet"
CLI_TIMEOUT_SECONDS = 300
API_MAX_TOKENS = 4096
API_DEFAULT_MODEL = "claude-sonnet-5-5"


class LlmError(RuntimeError):
    pass


class LlmClient(Protocol):
    model: str

    def complete(self, prompt: str) -> str: ...


class ClaudeCliClient:
    """Runs the local `claude` CLI in print mode with every tool disabled."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: int = CLI_TIMEOUT_SECONDS) -> None:
        self.model = model
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        command = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
        ]
        try:
            result = subprocess.run(
                command, input=prompt, capture_output=True, text=True, timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as error:
            raise LlmError(f"claude CLI failed: {error}") from error
        if result.returncode != 0:
            raise LlmError(f"claude CLI exited {result.returncode}: {result.stderr.strip()[:300]}")
        return parse_cli_output(result.stdout)


def parse_cli_output(stdout: str) -> str:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise LlmError(f"claude CLI returned non-JSON output: {stdout[:200]!r}") from error
    if envelope.get("is_error"):
        raise LlmError(f"claude CLI reported an error: {str(envelope.get('result'))[:300]}")
    text = envelope.get("result")
    if not isinstance(text, str):
        raise LlmError("claude CLI JSON has no string 'result'")
    return text


class AnthropicApiClient:
    """Same interface as ClaudeCliClient, backed by the Anthropic SDK (optional dependency)."""

    def __init__(self, model: str = API_DEFAULT_MODEL, max_tokens: int = API_MAX_TOKENS) -> None:
        try:
            import anthropic
        except ImportError as error:
            raise LlmError("the 'anthropic' package is not installed") from error
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LlmError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic()

    def complete(self, prompt: str) -> str:
        message = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")


def make_client(backend: str, model: str | None = None) -> LlmClient:
    if backend == "cli":
        return ClaudeCliClient(model) if model else ClaudeCliClient()
    if backend == "api":
        return AnthropicApiClient(model) if model else AnthropicApiClient()
    raise ValueError(f"unknown LLM backend: {backend!r} (use 'cli' or 'api')")
