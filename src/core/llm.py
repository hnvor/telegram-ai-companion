"""Anthropic Claude wrapper с prompt caching, tool use и логированием расхода."""

import json
from typing import Any

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from src.config import settings
from src.db.repo import UsageRepo

log = structlog.get_logger()

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _log_usage(user_id: int, model: str, usage, purpose: str) -> None:
    try:
        await UsageRepo.log(
            user_id=user_id,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            purpose=purpose,
        )
    except Exception as e:
        log.warning("usage.log_failed", error=str(e))


async def chat(
    *,
    user_id: int,
    system_static: str,
    system_dynamic: str,
    messages: list[MessageParam],
    model: str | None = None,
    max_tokens: int = 2048,
    purpose: str = "chat",
    temperature: float = 0.7,
) -> str:
    """Главная точка входа в LLM. Кеширует system_static + system_dynamic."""
    model = model or settings.llm_main_model
    client = get_client()

    system_blocks: list[dict[str, Any]] = []
    if system_static:
        system_blocks.append(
            {
                "type": "text",
                "text": system_static,
                "cache_control": {"type": "ephemeral"},
            }
        )
    if system_dynamic:
        system_blocks.append(
            {
                "type": "text",
                "text": system_dynamic,
                "cache_control": {"type": "ephemeral"},
            }
        )

    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,  # type: ignore[arg-type]
        messages=messages,
    )

    text_parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    text = "".join(text_parts).strip()

    await _log_usage(user_id, model, resp.usage, purpose)
    log.info(
        "llm.chat",
        model=model,
        purpose=purpose,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )

    return text


async def chat_with_tools(
    *,
    user_id: int,
    system_static: str,
    system_dynamic: str,
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    max_tokens: int = 2048,
    purpose: str = "chat_tools",
    temperature: float = 0.7,
    max_iterations: int = 5,
) -> str:
    """Tool-use loop: даём Claude список tools, он сам решает что вызвать.

    Импорт внутри — чтобы избежать циклической зависимости.
    """
    from src.core.tools import execute_tool

    model = model or settings.llm_main_model
    client = get_client()

    system_blocks: list[dict[str, Any]] = []
    if system_static:
        system_blocks.append(
            {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}}
        )
    if system_dynamic:
        system_blocks.append(
            {"type": "text", "text": system_dynamic, "cache_control": {"type": "ephemeral"}}
        )

    convo: list[dict] = list(messages)

    for iteration in range(max_iterations):
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_blocks,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            messages=convo,  # type: ignore[arg-type]
        )

        await _log_usage(user_id, model, resp.usage, purpose)

        if resp.stop_reason != "tool_use":
            text_parts = [
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ]
            return "".join(text_parts).strip()

        # Сохраняем assistant message целиком (включая tool_use блоки)
        assistant_blocks: list[dict] = []
        tool_use_blocks: list[tuple[str, str, dict]] = []  # (id, name, input)
        for b in resp.content:
            btype = getattr(b, "type", None)
            if btype == "text":
                assistant_blocks.append({"type": "text", "text": b.text})
            elif btype == "tool_use":
                assistant_blocks.append(
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                )
                tool_use_blocks.append((b.id, b.name, b.input))
        convo.append({"role": "assistant", "content": assistant_blocks})

        # Выполняем все tool_use блоки и собираем tool_result
        tool_results: list[dict] = []
        for tu_id, tu_name, tu_input in tool_use_blocks:
            try:
                out = await execute_tool(user_id, tu_name, tu_input or {})
            except Exception as e:
                out = {"error": str(e)}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": json.dumps(out, ensure_ascii=False, default=str)[:8000],
                }
            )

        convo.append({"role": "user", "content": tool_results})

    # Если зациклились — попросим финальный ответ без tools
    log.warning("llm.tools_max_iterations", iterations=max_iterations)
    final = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,  # type: ignore[arg-type]
        messages=convo + [
            {"role": "user", "content": "Без новых tool-вызовов: дай итоговый ответ пользователю на основе уже собранных данных."}
        ],
    )
    await _log_usage(user_id, model, final.usage, purpose + "_finalize")
    text_parts = [b.text for b in final.content if getattr(b, "type", None) == "text"]
    return "".join(text_parts).strip()


async def chat_json(
    *,
    user_id: int,
    system: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 1024,
    purpose: str = "extraction",
) -> str:
    """Один запрос без кеша, оптимизирован под фоновые задачи (extraction, etc.)."""
    model = model or settings.llm_cheap_model
    client = get_client()

    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.2,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = "".join(text_parts).strip()

    await _log_usage(user_id, model, resp.usage, purpose)

    return text
