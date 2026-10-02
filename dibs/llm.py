"""Model client. Anything that speaks the OpenAI chat-completions format works:
Gemini's free tier (default), a local Ollama, Groq, or a paid provider later.
"""

from typing import Protocol

from . import config


class LLMUnavailable(Exception):
    """Every configured model failed with a temporary or retired-model error."""


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """Return the assistant message as a dict with 'content' and optional 'tool_calls'."""


class OpenAICompatLLM:
    def __init__(self, base_url: str = config.LLM_BASE_URL, api_key: str = config.LLM_API_KEY, model: str = config.LLM_MODEL):
        from openai import OpenAI

        if not api_key:
            raise SystemExit("LLM_API_KEY is not set. Copy .env.example to .env and add a free Gemini key.")
        # No SDK retries and a short timeout: when a model is busy or out of free quota,
        # move to the next model at once instead of making the user wait.
        self.client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=45)
        self.model = model
        self.models = [model] + [m for m in config.LLM_FALLBACK_MODELS if m != model]

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        last_error = None
        for model in self.models:  # free tiers get busy or retire models: move to the next one
            try:
                extra = {"tools": tools} if tools else {}
                resp = self.client.chat.completions.create(model=model, messages=messages, temperature=0.3, **extra)
                return resp.choices[0].message.model_dump(exclude_none=True)
            except APIStatusError as exc:
                if exc.status_code not in (404, 429, 500, 503):
                    raise
                last_error = exc
            except (APITimeoutError, APIConnectionError) as exc:
                last_error = exc
        raise LLMUnavailable(str(last_error))

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list())


if __name__ == "__main__":
    for name in OpenAICompatLLM().list_models():
        print(name)
