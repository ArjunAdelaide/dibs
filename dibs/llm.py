"""Model router. Each job has its own ordered list of models: the first choice, then backups.

    chat      talks to the user and picks tools: fast and cheap
    browser   drives the browser agent: the strongest model you can afford
    memory    the daily tidy-up of memory files: cheap

Any provider that speaks the OpenAI chat-completions format works (Gemini,
OpenRouter, Groq, Together, a local Ollama, your own server). Set them in .env:

    PROVIDER_OPENROUTER_URL=https://openrouter.ai/api/v1
    PROVIDER_OPENROUTER_KEY=...
    LLM_CHAT=gemini-flash-lite-latest, gemini-3.5-flash-lite
    LLM_BROWSER=openrouter@some/strong-model, gemini-3.5-flash

A model is written `provider@model`, or just `model` for the default provider.
When a model is busy, out of quota, retired or times out, the next one is tried at once.

    python -m dibs.llm            # show the model list for each job
    python -m dibs.llm --models   # list the models your default key can use
"""

from typing import Callable, Protocol

from . import config

JOBS = ("chat", "browser", "memory")


class LLMUnavailable(Exception):
    """Every model in the list failed with a temporary or retired-model error."""


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """Return the assistant message as a dict with 'content' and optional 'tool_calls'."""


def parse_chain(chain: list[str]) -> list[tuple[str, str]]:
    """['openrouter@a/b:free', 'gemini-x'] -> [('openrouter', 'a/b:free'), ('gemini', 'gemini-x')]"""
    steps = []
    for item in chain:
        provider, at, model = item.partition("@")
        steps.append((provider.lower(), model) if at else (config.DEFAULT_PROVIDER, item))
    return steps


def _openai_client(provider: str):
    from openai import OpenAI

    url, key = config.PROVIDERS.get(provider, ("", ""))
    if not key:
        hint = "Copy .env.example to .env and add a free Gemini key." if provider == config.DEFAULT_PROVIDER else \
            f"Add PROVIDER_{provider.upper()}_URL and PROVIDER_{provider.upper()}_KEY to .env."
        raise SystemExit(f"No API key for model provider '{provider}'. {hint}")
    # No SDK retries and a short timeout: when a model is busy or out of quota,
    # move to the next model at once instead of making the user wait.
    return OpenAI(base_url=url, api_key=key, max_retries=0, timeout=45)


class Router:
    def __init__(self, job: str = "chat", chain: list[str] | None = None, client_for: Callable = _openai_client):
        self.job_name = job
        self.steps = parse_chain(chain or config.LLM_CHAINS[job])
        self._client_for = client_for
        self._clients: dict[str, object] = {}
        self.used: dict[str, int] = {}  # how often each model answered, for cost and quality checks
        for provider, _ in self.steps:  # fail at start-up, not in the middle of a chat
            self._client(provider)

    def _client(self, provider: str):
        if provider not in self._clients:
            self._clients[provider] = self._client_for(provider)
        return self._clients[provider]

    def job(self, name: str) -> "Router":
        """The router for another job, sharing nothing but the settings."""
        return for_job(name)

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        last_error = None
        for provider, model in self.steps:
            try:
                extra = {"tools": tools} if tools else {}
                resp = self._client(provider).chat.completions.create(model=model, messages=messages, temperature=0.3, **extra)
                self.used[f"{provider}@{model}"] = self.used.get(f"{provider}@{model}", 0) + 1
                return resp.choices[0].message.model_dump(exclude_none=True)
            except APIStatusError as exc:
                if exc.status_code not in (404, 429, 500, 502, 503):
                    raise
                last_error = exc
            except (APITimeoutError, APIConnectionError) as exc:
                last_error = exc
        raise LLMUnavailable(str(last_error))

    def describe(self) -> str:
        return " -> ".join(f"{p}@{m}" for p, m in self.steps)


_routers: dict[str, Router] = {}


def for_job(job: str) -> Router:
    if job not in _routers:
        _routers[job] = Router(job)
    return _routers[job]


if __name__ == "__main__":
    import sys

    if "--models" in sys.argv:
        for model in sorted(m.id for m in _openai_client(config.DEFAULT_PROVIDER).models.list()):
            print(model)
    else:
        print("providers:", ", ".join(sorted(config.PROVIDERS)))
        for name in JOBS:
            print(f"{name:8} {for_job(name).describe()}")
