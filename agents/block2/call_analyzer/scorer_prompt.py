"""
Будує промпти для AI-оцінки транскриптів дзвінків (Модуль 2.2).
"""

import json

from agents.block2.call_analyzer.config_reader import Criterion, MarkerPhrase

# Промпт для транскрипції аудіо
TRANSCRIPTION_PROMPT = """\
Транскрибуй аудіозапис телефонного дзвінка.

Вимоги:
1. Розмітка мовців: кожна репліка починається з [ОПЕРАТОР]: або [КЛІЄНТ]:
2. Мова — як є в записі (не перекладати).
3. Нечіткі слова позначай [...].
4. Тривалі паузи позначай [ПАУЗА].
5. НЕ додавай коментарів чи пояснень — тільки транскрипт.

Формат:
[ОПЕРАТОР]: текст першої репліки
[КЛІЄНТ]: текст відповіді
[ОПЕРАТОР]: ...
"""


def build_scoring_prompt(
    transcript: str,
    criteria: list[Criterion],
    markers: list[MarkerPhrase],
    call_meta: dict,
) -> str:
    """
    Будує промпт для AI-оцінки транскрипту за критеріями.

    Returns:
        Рядок промпту. Gemini має повернути JSON відповідно до схеми нижче.
    """
    criteria_block = _format_criteria(criteria)
    markers_block  = _format_markers(markers)
    meta_block     = _format_meta(call_meta)

    return f"""\
Ти — AI-аудитор якості колл-центру медичного центру.
Проаналізуй транскрипт телефонного дзвінка і оціни роботу оператора.

{meta_block}

─── ТРАНСКРИПТ ───
{transcript}

─── КРИТЕРІЇ ОЦІНКИ ───
{criteria_block}

{markers_block}

─── ЗАВДАННЯ ───
1. Оціни кожен критерій: виконано (true) чи ні (false).
2. Відзнач які фрази-маркери були або не були вжиті.
3. Напиши короткий (2-4 речення) коментар AI українською.
4. Порахуй загальну оцінку як відношення виконаних критеріїв до загальної кількості (0.0–1.0).

Відповідь ТІЛЬКИ у форматі JSON (без markdown-обгортки):
{{
  "criteria_scores": {{
    "Назва критерію 1": true,
    "Назва критерію 2": false,
    ...
  }},
  "marker_violations": ["Маркер якого не було", ...],
  "marker_used": ["Маркер який був використаний", ...],
  "ai_comment": "Короткий коментар українською...",
  "total_score": 0.75
}}
"""


def _format_criteria(criteria: list[Criterion]) -> str:
    if not criteria:
        return "Критерії не визначено."
    lines = []
    current_block = ""
    for c in criteria:
        if c.block and c.block != current_block:
            current_block = c.block
            lines.append(f"\n[{current_block}]")
        indicator = f" — {c.indicator}" if c.indicator else ""
        weight_s = f" (вага: {c.weight})" if c.weight != 1.0 else ""
        lines.append(f"  • {c.name}{indicator}{weight_s}")
    return "\n".join(lines).strip()


def _format_markers(markers: list[MarkerPhrase]) -> str:
    if not markers:
        return ""
    lines = ["─── ФРАЗИ-МАРКЕРИ (обов'язкові фрази) ───"]
    for m in markers:
        cond = f" [{m.condition}]" if m.condition else ""
        lines.append(f"  • «{m.phrase}»{cond}")
    return "\n".join(lines)


def _format_meta(call_meta: dict) -> str:
    dt  = call_meta.get("dateTime", call_meta.get("callDate", ""))
    dur = call_meta.get("billsec", call_meta.get("duration", "?"))
    drn = call_meta.get("direction", "")
    line = call_meta.get("internalNumber", call_meta.get("operatorLine", ""))
    op  = call_meta.get("operatorName", "")

    parts = [f"─── МЕТАДАНІ ДЗВІНКА ───"]
    if dt:
        parts.append(f"Дата/час: {dt}")
    if line:
        parts.append(f"Лінія: {line}")
    if op:
        parts.append(f"Оператор: {op}")
    if dur:
        parts.append(f"Тривалість: {dur} с")
    if drn:
        dir_ua = "вхідний" if drn in ("inbound", "incoming") else "вихідний"
        parts.append(f"Напрям: {dir_ua}")
    return "\n".join(parts)


def parse_scoring_response(json_text: str, criteria: list[Criterion]) -> dict:
    """
    Розпарсити JSON відповідь Gemini.
    При помилці парсингу — повертає безпечний fallback.
    """
    try:
        # Gemini іноді обгортає JSON в ```json ... ```
        text = json_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])
        data = json.loads(text)
        # Валідація: переконуємось що всі критерії є в scores
        scores = data.get("criteria_scores", {})
        for c in criteria:
            if c.name not in scores:
                scores[c.name] = False
        data["criteria_scores"] = scores
        return data
    except Exception:
        # Fallback: всі критерії = False
        scores = {c.name: False for c in criteria}
        return {
            "criteria_scores": scores,
            "marker_violations": [],
            "marker_used": [],
            "ai_comment": "Не вдалося розпарсити відповідь AI.",
            "total_score": 0.0,
        }
