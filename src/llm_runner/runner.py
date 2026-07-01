"""Minimal multi-provider LLM client used by the ContextLadder runner.

`setup_client(provider)` returns an OpenAI-compatible or Anthropic client, and
`send_prompt(client, context, user_input, model)` sends a (system, user) prompt
pair and returns ``(answer_text, reasoning_text, usage)``.

Provider/model coupling: Claude models must use ``provider="anthropic"`` (the
``"claude" in model`` branch uses the Anthropic SDK); DeepSeek / OpenAI / other
OpenAI-compatible models use the OpenAI SDK with the provider's base URL.

API keys are read from the environment (see `.env.example`):
``OPENAI_API_KEY``, ``DEEPSEEK_API_KEY``, ``ANTHROPIC_API_KEY``,
``OPENROUTER_API_KEY``.
"""

import os

import anthropic
from openai import OpenAI

MODEL_PROVIDERS = {
    "openai": {"api_key_env": "OPENAI_API_KEY", "base_url": None},
    "deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "base_url": "https://api.deepseek.com"},
    "anthropic": {"api_key_env": "ANTHROPIC_API_KEY", "base_url": None},
    "openrouter": {"api_key_env": "OPENROUTER_API_KEY", "base_url": "https://openrouter.ai/api/v1"},
}


def setup_client(provider: str):
    if provider not in MODEL_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")
    if provider == "anthropic":
        return anthropic.Anthropic()
    info = MODEL_PROVIDERS[provider]
    return OpenAI(api_key=os.getenv(info["api_key_env"]), base_url=info["base_url"])


def extract_text(message) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


def send_prompt(client, context: str, user_input: str, model: str,
                enable_thinking: bool = False):
    """Send a (system=context, user=user_input) prompt; return (answer, reasoning, usage)."""
    messages = [
        {"role": "system", "content": context},
        {"role": "user", "content": user_input},
    ]
    usage = None
    answer_content = ""
    reasoning_content = ""
    extra = {"extra_body": {"enable_thinking": True}} if enable_thinking else {}

    if "claude" in model:
        with client.messages.stream(
            model=model,
            temperature=0.7,
            system=context,
            max_tokens=64_000,
            messages=[{"role": "user", "content": user_input}],
        ) as stream:
            stream.until_done()
            final_message = stream.get_final_message()
            if final_message:
                answer_content = extract_text(final_message)
                u = final_message.usage
                usage = {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "total_tokens": u.input_tokens + u.output_tokens,
                }
        return answer_content, reasoning_content, usage

    if model.startswith("gpt-5"):
        response = client.chat.completions.create(
            model=model, messages=messages, stream=False, **extra)
    elif "llama" in model:
        response = client.chat.completions.create(
            model=model, temperature=0.7,
            response_format={"type": "json_object"}, messages=messages)
    else:
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0.7, stream=False, **extra)

    answer_content = response.choices[0].message.content
    try:
        reasoning_content = response.choices[0].message.reasoning_content or ""
    except AttributeError:
        reasoning_content = ""
    try:
        u = response.usage
        usage = {
            "input_tokens": u.prompt_tokens,
            "output_tokens": u.completion_tokens,
            "total_tokens": u.total_tokens,
        }
    except AttributeError:
        usage = None

    return answer_content, reasoning_content, usage
