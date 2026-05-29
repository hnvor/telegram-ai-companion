"""LLM-гейт перед отправкой проактивного сообщения.

Перед каждым пушем спрашиваем Haiku — стоит ли вообще сейчас грузить пользователя?
Если нет (выжат/тяжёлый день/в автопилоте) — пропускаем тихо.
"""
import json
import re
from dataclasses import dataclass

import structlog

from src.core.llm import chat_json
from src.core.prompts import PROACTIVE_GATE_PROMPT
from src.core.signals import Signals
from src.db.client import get_pool

log = structlog.get_logger()


@dataclass
class GateDecision:
    send: bool
    reason: str
    soften: str = ""


async def decide_proactive(
    user_id: int, kind: str, signals: Signals
) -> GateDecision:
    """Спрашивает Haiku: слать или нет конкретный тип пуша сейчас."""

    # Hard rules перед LLM — экономим токены на очевидном
    if signals.pressure < 0.20 and kind in ("habit_nudge", "challenge"):
        return GateDecision(send=False, reason="pressure too low for non-critical push")
    if signals.engagement < 0.15 and kind in ("habit_nudge", "challenge", "anchor"):
        return GateDecision(send=False, reason="engagement collapsed, not pushing")

    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT role, content, created_at FROM conversations "
        "WHERE user_id=$1 AND role='user' "
        "ORDER BY created_at DESC LIMIT 8",
        user_id,
    )
    convo = "\n".join(
        f"[{r['created_at']:%m-%d %H:%M}] {(r['content'] or '')[:300]}"
        for r in reversed(rows)
    ) or "Сообщений нет."

    user_message = (
        f"pressure={signals.pressure:.2f}\n"
        f"engagement={signals.engagement:.2f}\n"
        f"signals_breakdown={signals.note}\n"
        f"proposed_kind={kind}\n\n"
        f"## Последние сообщения пользователя\n{convo}\n\n"
        "Решай: send или нет?"
    )

    try:
        raw = await chat_json(
            user_id=user_id,
            system=PROACTIVE_GATE_PROMPT,
            user_message=user_message,
            purpose="proactive_gate",
            max_tokens=200,
        )
    except Exception as e:
        log.warning("gate.llm_failed", error=str(e))
        # При ошибке — пропускаем неcritical, остальное шлём
        if kind in ("habit_nudge", "challenge", "anchor"):
            return GateDecision(send=False, reason=f"gate failed: {e}")
        return GateDecision(send=True, reason="gate failed but kind is critical")

    data = _parse_json_loose(raw)
    if data is None:
        log.warning("gate.invalid_json", raw=raw[:200])
        # При непарсе по умолчанию НЕ слать неcritical — лучше тишина чем шум
        if kind in ("habit_nudge", "challenge", "anchor"):
            return GateDecision(send=False, reason="gate parse failed → silent")
        return GateDecision(send=True, reason="gate parse failed, critical kind")

    decision = GateDecision(
        send=bool(data.get("send", True)),
        reason=str(data.get("reason", ""))[:200],
        soften=str(data.get("soften", ""))[:200],
    )
    log.info(
        "gate.decision", user_id=user_id, kind=kind,
        send=decision.send, reason=decision.reason,
    )
    return decision


_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _parse_json_loose(raw: str) -> dict | None:
    """Достаёт первый валидный JSON-объект из ответа LLM, игнорируя
    обрамление ```json…``` и любой хвостовой текст после."""
    text = raw.strip()
    # снимаем code fence
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if "```" in text:
            text = text.split("```", 1)[0]
        text = text.strip()
    # быстрый путь
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    # ищем первый {…}
    for m in _JSON_OBJECT_RE.finditer(text):
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None
