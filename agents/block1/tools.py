"""
Function calling tools для Gemini Live API — Блок 1: Нагадування про візити.

Gemini викликає функції під час розмови.
Ми виконуємо їх і повертаємо результат назад в сесію.
"""

import logging
import time
from datetime import datetime
from typing import Any

from google.genai import types

from config import GEMINI_LOGS, LOG_FILE
from services import clinic_api

logger = logging.getLogger(__name__)

_MONTHS_UA = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]

_WEEKDAYS_UA = [
    "понеділок", "вівторок", "середа", "четвер",
    "п'ятниця", "субота", "неділя",
]


def _fmt_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return f"{dt.day} {_MONTHS_UA[dt.month - 1]} о {dt.strftime('%H:%M')}"


def _slot_period(hour: int) -> str:
    """Розбиваємо день на 3 частини: ранок / обід / вечір."""
    if hour < 12:
        return "ранок"
    elif hour < 17:
        return "обід"
    else:
        return "вечір"


_UA_ORDINALS = {
    1: "перше", 2: "друге", 3: "третє", 4: "четверте", 5: "п'яте",
    6: "шосте", 7: "сьоме", 8: "восьме", 9: "дев'яте", 10: "десяте",
    11: "одинадцяте", 12: "дванадцяте", 13: "тринадцяте", 14: "чотирнадцяте",
    15: "п'ятнадцяте", 16: "шістнадцяте", 17: "сімнадцяте", 18: "вісімнадцяте",
    19: "дев'ятнадцяте", 20: "двадцяте",
    21: "двадцять перше", 22: "двадцять друге", 23: "двадцять третє",
    24: "двадцять четверте", 25: "двадцять п'яте", 26: "двадцять шосте",
    27: "двадцять сьоме", 28: "двадцять восьме", 29: "двадцять дев'яте",
    30: "тридцяте", 31: "тридцять перше",
}
# Зворотній словник: "дванадцяте" → 12 (для числового fallback у матчингу)
_NUM_FROM_ORDINAL: dict[str, int] = {v: k for k, v in _UA_ORDINALS.items()}


def _ua_ordinal(day: int) -> str:
    """Повний порядковий числівник середнього роду: 14 → 'чотирнадцяте'."""
    return _UA_ORDINALS.get(day, f"{day}-те")


def _natural_date_list(date_summaries: list[dict]) -> str:
    """
    Будує природній перелік дат для вимови AI — дати вже в ordinal форматі.

    Один місяць:  "дванадцяте, тринадцяте або чотирнадцяте жовтня"
    Різні місяці: "дванадцяте жовтня або п'яте листопада"
    """
    by_month: dict[str, list[str]] = {}
    order: list[str] = []
    for d in date_summaries:
        parts = d["date"].split()
        day_word = parts[0]   # вже ordinal: "дванадцяте"
        month = parts[1] if len(parts) > 1 else ""
        if month not in by_month:
            by_month[month] = []
            order.append(month)
        by_month[month].append(day_word)

    segments = []
    for month in order:
        items = by_month[month]
        if len(items) == 1:
            segments.append(f"{items[0]} {month}")
        else:
            segments.append(f"{', '.join(items[:-1])} або {items[-1]} {month}")
    return ", ".join(segments)


def _natural_date_list_with_weekday(date_summaries: list[dict]) -> str:
    """
    Будує перелік дат з днями тижня для вимови AI.
    Формат: "вівторок тринадцяте, середа чотирнадцяте або четвер п'ятнадцяте жовтня"
    date_summaries повинен мати поля: date (ordinal), weekday (str).
    """
    by_month: dict[str, list[str]] = {}
    order: list[str] = []
    for d in date_summaries:
        parts = d["date"].split()
        day_word = parts[0]
        month = parts[1] if len(parts) > 1 else ""
        weekday = d.get("weekday", "")
        entry = f"{weekday} {day_word}" if weekday else day_word
        if month not in by_month:
            by_month[month] = []
            order.append(month)
        by_month[month].append(entry)

    segments = []
    for month in order:
        items = by_month[month]
        if len(items) == 1:
            segments.append(f"{items[0]} {month}")
        else:
            segments.append(f"{', '.join(items[:-1])} або {items[-1]} {month}")
    return ", ".join(segments)


# Мінімальна кількість слотів у частині дня для показу діапазону замість переліку
_PERIOD_THRESHOLD = 6


# ---------------------------------------------------------------------------
# Визначення інструментів для Gemini
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        function_declarations=[

            types.FunctionDeclaration(
                name="confirm_visit",
                description=(
                    "Підтвердити візит пацієнта. "
                    "Викликати коли пацієнт чітко підтвердив що прийде."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID візиту для підтвердження",
                        ),
                    },
                    required=["visit_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_available_slots",
                description=(
                    "Отримати доступні дати або вільний час для запису до лікаря. "
                    "БЕЗ параметра date: повертає список доступних ДАТ (без конкретних часів). "
                    "Викликати ОДРАЗУ як пацієнт сказав що хоче перенести. "
                    "З параметром date: повертає конкретний вільний час на обрану дату. "
                    "Викликати ПІСЛЯ того як пацієнт обрав дату. "
                    "НЕ викликати повторно якщо вільний час для цієї дати вже отримано — "
                    "пацієнт вже бачив список, одразу виклич reschedule_visit з потрібним slot_id."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "date": types.Schema(
                            type="STRING",
                            description=(
                                "Дата для отримання доступного часу (напр. '12 жовтня'). "
                                "Якщо пацієнт назвав день тижня ('понеділок') — "
                                "передай саме день тижня: система автоматично знайде потрібну дату. "
                                "Якщо не вказана — повертає тільки список доступних дат."
                            ),
                        ),
                    },
                ),
            ),

            types.FunctionDeclaration(
                name="reschedule_visit",
                description=(
                    "Перенести візит на обраний пацієнтом час. "
                    "Викликати лише після того як пацієнт підтвердив конкретний час."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID поточного візиту",
                        ),
                        "slot_id": types.Schema(
                            type="STRING",
                            description="ID вільного часу з результату get_available_slots",
                        ),
                    },
                    required=["visit_id", "slot_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="cancel_visit",
                description=(
                    "Скасувати візит пацієнта. "
                    "Викликати ЛИШЕ якщо пацієнт явно відмовився від переносу або чітко попросив скасувати."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "visit_id": types.Schema(
                            type="STRING",
                            description="ID візиту для скасування",
                        ),
                        "reason": types.Schema(
                            type="STRING",
                            description="Причина скасування якщо пацієнт вказав",
                        ),
                    },
                    required=["visit_id"],
                ),
            ),

types.FunctionDeclaration(
                name="end_call",
                description=(
                    "Завершити розмову. "
                    "ОБОВ'ЯЗКОВО спочатку ВГОЛОС скажи прощання, лише потім виклич end_call. "
                    "confirmed — пацієнт підтвердив запис; "
                    "rescheduled — запис перенесено; "
                    "cancelled — запис скасовано; "
                    "no_result — пацієнт не визначився або не зрозумів. "
                    "  Перед end_call(no_result) ОБОВ'ЯЗКОВО скажи вголос: "
                    "  'Вибачте, не вдалося порозумітись. Наш менеджер зв'яжеться з вами. На все добре!'; "
                    "no_answer — пацієнт не відповів на дзвінок; "
                    "wrong_number — пацієнт каже що не записувався або ви помилилися номером. "
                    "  Перед end_call(wrong_number) ОБОВ'ЯЗКОВО скажи вголос: "
                    "  'Добре, вибачте за турбування. На все добре!'"
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "result": types.Schema(
                            type="STRING",
                            description="Підсумок розмови",
                            enum=["confirmed", "rescheduled", "cancelled", "no_result", "no_answer", "wrong_number"],
                        ),
                    },
                    required=["result"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_preparation_info",
                description=(
                    "Отримати інформацію про підготовку до процедури/послуги та її вартість. "
                    "Викликати коли пацієнт питає: 'що взяти з собою', 'як підготуватись', "
                    "'що потрібно', 'скільки коштує', 'яка ціна', 'що треба зробити перед прийомом'. "
                    "НЕ викликати для запису — тільки для консультації."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "service_name": types.Schema(
                            type="STRING",
                            description=(
                                "Назва процедури або послуги яку питає пацієнт. "
                                "Передай офіційну назву або ключові слова (наприклад 'гастроентеролог', 'УЗД', 'ВГДС')."
                            ),
                        ),
                    },
                    required=["service_name"],
                ),
            ),

            types.FunctionDeclaration(
                name="handle_record_error",
                description=(
                    "Обробити ситуацію коли пацієнт каже що запис є помилкою або що вже відвідував. "
                    "Викликати при фразах: 'це помилка', 'я вже був', 'вже приходив', 'щось переплутали', "
                    "'не той час', 'не та дата', 'я думала на інше', 'мабуть помилка'."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={},
                ),
            ),

            types.FunctionDeclaration(
                name="transfer_to_operator",
                description=(
                    "Перемкнути дзвінок на менеджера (лінія 914). "
                    "ОБОВ'ЯЗКОВИЙ алгоритм перед викликом: "
                    "1) СПОЧАТКУ скажи пацієнту 'Бажаєте з'єднати з менеджером?' "
                    "2) МОВЧКИ ЧЕКАЙ відповідь пацієнта. "
                    "3) Тільки якщо пацієнт явно сказав 'так' / 'бажаю' / 'з'єднайте' — "
                    "скажи 'Добре, перенаправляю ваш дзвінок до менеджера. До побачення!' і виклич цю функцію. "
                    "ЗАБОРОНЕНО: питати 'Бажаєте?' і одразу викликати без очікування відповіді. "
                    "ЗАБОРОНЕНО: викликати якщо пацієнт не сказав явного 'так'."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "reason": types.Schema(
                            type="STRING",
                            description="Причина передачі (для логування)",
                        ),
                    },
                    required=["reason"],
                ),
            ),

        ]
    )
]


# ---------------------------------------------------------------------------
# Обробка викликів
# ---------------------------------------------------------------------------

# Функції які можна викликати лише 1 раз за дзвінок
_ONE_TIME_TOOLS = {"confirm_visit", "cancel_visit", "reschedule_visit", "end_call", "transfer_to_operator"}
# TTL кешу для get_available_slots (секунди)
_SLOTS_CACHE_TTL = 60


async def handle_tool_call(
    function_name: str,
    args: dict,
    visit_context: dict,
    call_cache: dict | None = None,
) -> dict[str, Any]:
    """
    Виконати tool call від Gemini та повернути результат.
    call_cache — словник що живе протягом одного дзвінка, захищає від дублів.

    Returns:
        dict який Gemini отримає як відповідь функції
    """
    if call_cache is None:
        call_cache = {}

    # Захист від дублікатів
    if function_name in _ONE_TIME_TOOLS and function_name in call_cache:
        logger.warning(f"[TOOL] {function_name} вже викликався — повертаємо кешований результат")
        return call_cache[function_name]

    if function_name == "get_available_slots":
        requested_date = args.get("date", "").strip()
        cache_key = f"get_available_slots:{requested_date}" if requested_date else "get_available_slots"
        cached = call_cache.get(cache_key)
        if cached and time.monotonic() - cached["_ts"] < _SLOTS_CACHE_TTL:
            logger.warning(f"[TOOL] get_available_slots — кеш '{cache_key}' (TTL {_SLOTS_CACHE_TTL}с)")
            if requested_date:
                cache_age = time.monotonic() - cached["_ts"]
                if cache_age < 3.0:
                    # Подвійний виклик у тому ж батчі (Gemini artifact, < 3с від першого виклику).
                    # СТОП тут хибний: пацієнт ще не бачив слоти і не називав конкретний час.
                    # Повертаємо звичайний результат щоб ghost-тур не казав "яку годину?"
                    # замість правильного переліку слотів.
                    logger.debug(f"[TOOL] get_available_slots — той самий батч ({cache_age:.2f}с) → без СТОП")
                    return cached["result"]
                # Повторний виклик з окремого туру — пацієнт вже бачив ці слоти
                # і щойно назвав конкретний час. Підказуємо Gemini одразу reschedule.
                result = {
                    **cached["result"],
                    "message": (
                        "СТОП. Доступний час вже було показано пацієнту. "
                        "Пацієнт щойно назвав конкретний час — знайди потрібний slot_id "
                        "у списку slots[] і ОДРАЗУ виклич reschedule_visit(visit_id=..., slot_id=...). "
                        "НЕ перераховуй доступний час знову. НЕ питай ще раз який час."
                    ),
                }
                return result
            return cached["result"]

    logger.info(f"[TOOL] {function_name}({args})")

    if function_name == "confirm_visit":
        success = await clinic_api.confirm_visit(args["visit_id"])
        result = {
            "success": success,
            "message": (
                "Візит підтверджено. Тепер ОДРАЗУ виклич end_call(result='confirmed'). НЕ питай нічого більше."
                if success else
                "Помилка підтвердження. Скажи пацієнту і заверши дзвінок."
            ),
        }
        call_cache["confirm_visit"] = result
        return result

    if function_name == "get_available_slots":
        # Використовуємо prefetch-кеш якщо він вже є (заповнений під час привітання).
        # Другий виклик (з датою) також бере з кешу — без повторного HTTP запиту.
        raw = call_cache.get("__raw_slots")
        if raw is None:
            raw = await clinic_api.get_doctor_slots(visit_context["visit_id"])
            if raw:
                call_cache["__raw_slots"] = raw
        if not raw:
            return {
                "success": False,
                "available_dates": [],
                "message": (
                    "⛔ ВІЛЬНИХ СЛОТІВ НЕМАЄ — API клініки повернув порожній список.\n"
                    "АБСОЛЮТНА ЗАБОРОНА: НЕ ВИГАДУЙ жодних дат, днів тижня або часу.\n"
                    "Дій СТРОГО:\n"
                    "1. Скажи: 'На жаль, наразі вільного часу для переносу немає.'\n"
                    "2. Запитай: 'Можу з'єднати з менеджером або залишити поточний запис. Що зручніше?'\n"
                    "3. 'менеджер/з'єднайте' → transfer_to_operator\n"
                    "4. 'залишити/прийду/так' → confirm_visit → end_call(confirmed)\n"
                    "5. 'скасуйте/відміняйте' → cancel_visit → end_call(cancelled)\n"
                    "6. ОДНЕ 'ні' → перепитай: 'Тобто скасуємо запис чи Ви плануєте прийти?'\n"
                    "   - 'прийду/так' → confirm_visit → end_call(confirmed)\n"
                    "   - 'скасуйте' → cancel_visit → end_call(cancelled)\n"
                    "НЕ завершуй дзвінок одразу — дай пацієнту обрати."
                ),
            }

        # Групуємо слоти по датах.
        visit_dt = datetime.fromisoformat(visit_context["visit_datetime"])
        dates_map: dict[str, list[dict]] = {}
        _dates_weekday: dict[str, str] = {}  # date_str → день тижня
        for item in raw:
            dt = datetime.fromisoformat(item["START_E"])
            date_str = f"{_ua_ordinal(dt.day)} {_MONTHS_UA[dt.month - 1]}"
            if date_str not in _dates_weekday:
                _dates_weekday[date_str] = _WEEKDAYS_UA[dt.weekday()]
            time_str = f"{dt.hour}:{dt.minute:02d}"  # без ведучого нуля: 8:30, 9:00
            period = _slot_period(dt.hour)
            slot = {
                "slot_id": str(item["ID"]),
                "date": date_str,
                "time": time_str,
                "period": period,
            }
            dates_map.setdefault(date_str, []).append(slot)

        requested_date = args.get("date", "").strip()

        if requested_date:
            # КРОК 2: пацієнт обрав дату → повертаємо слоти тільки за цю дату

            # Захист: якщо початковий перелік дат вже надавався — перевіряємо одразу
            # чи запитана дата є в ньому. Без цього get_available_slots(date=16) шукає
            # в raw_slots і знаходить реальні записи (16-те реально є в БД клініки,
            # але поза першими 3 датами які ми показали пацієнту) → AI суперечить сама
            # собі (спочатку "є тільки 12, 13, 14", потім "на 16 є слоти вранці та в обід").
            _initial_call = call_cache.get("get_available_slots")
            if _initial_call:
                _initial_dates = [d["date"] for d in _initial_call["result"].get("available_dates", [])]
                _req_l = requested_date.lower().strip()
                _found = False
                for _id in _initial_dates:
                    if _req_l == _id.lower() or _req_l in _id.lower():
                        _found = True
                        break
                if not _found:
                    # Числовий fallback: "16", "16 жовтня", "пацієнт обрав 16", "п'ятниця 25 вересня" →
                    # шукаємо будь-яке слово в рядку що є числом (int), не лише split()[0]
                    _req_day = None
                    for _word in _req_l.split():
                        try:
                            _req_day = int(_word)
                            break
                        except ValueError:
                            pass
                    if _req_day is not None:
                        for _id in _initial_dates:
                            # ordinal може бути складеним ("двадцять перше квітня") —
                            # беремо всі слова крім останнього (місяця)
                            _ordinal_part = " ".join(_id.split()[:-1])
                            if _NUM_FROM_ORDINAL.get(_ordinal_part) == _req_day:
                                _found = True
                                break
                if not _found:
                    # День тижня fallback: "п'ятниця" / "п'ятниця 25 вересня" → конкретна дата
                    # Перевіряємо і рівність і входження (Gemini часто додає число до дня тижня)
                    for _d in _initial_call["result"].get("available_dates", []):
                        _wd = _d.get("weekday", "").lower()
                        if _wd and (_wd == _req_l or _wd in _req_l):
                            logger.info(
                                f"[TOOL] get_available_slots: день тижня '{_wd}' → дата '{_d['date']}'"
                            )
                            requested_date = _d["date"]
                            _found = True
                            break
                if not _found:
                    _available_dates = _initial_call["result"].get("available_dates", [])
                    _dates_str = ", ".join(
                        f"{d.get('weekday', '')} {d['date']}".strip()
                        for d in _available_dates
                    ) if _available_dates else "відсутні"
                    logger.info(
                        f"[TOOL] get_available_slots(date={requested_date}) → поза переліком → відхилено"
                    )
                    return {
                        "success": False,
                        "message": (
                            f"На жаль, на {requested_date} вільних місць немає — "
                            f"ця дата поза нашим переліком. "
                            f"Є тільки: {_dates_str}. "
                            f"Скажи пацієнту це і запитай яку з цих дат обрати."
                        ),
                    }

            # Пошук дати: спочатку точний збіг, потім "запит є підрядком ключа"
            # НЕ використовуємо "ключ є підрядком запиту" — це викликає баг:
            # "сімнадцяте" є підрядком "вісімнадцяте" → 17-е матчиться замість 18-го.
            matched_slots = None
            matched_date_key = ""
            _req = requested_date.lower().strip()
            for d, slots in dates_map.items():
                if _req == d.lower():          # точний збіг
                    matched_slots = slots
                    matched_date_key = d
                    break
            if not matched_slots:
                for d, slots in dates_map.items():
                    if _req in d.lower():      # запит є підрядком ключа ("вісімнадцяте" in "вісімнадцяте липня")
                        matched_slots = slots
                        matched_date_key = d
                        break
            if not matched_slots:
                # Числовий fallback: "12", "12 жовтня", "пацієнт обрав 12", "п'ятниця 25 вересня" →
                # шукаємо будь-яке слово в рядку що є числом, не лише split()[0]
                _req_day2 = None
                for _word in _req.split():
                    try:
                        _req_day2 = int(_word)
                        break
                    except ValueError:
                        pass
                if _req_day2 is not None:
                    for d, slots in dates_map.items():
                        _ordinal_part = " ".join(d.split()[:-1])
                        if _NUM_FROM_ORDINAL.get(_ordinal_part) == _req_day2:
                            matched_slots = slots
                            matched_date_key = d
                            break
            if not matched_slots:
                return {
                    "success": False,
                    "message": f"На {requested_date} вільного часу немає. "
                               f"Доступні дати: {', '.join(dates_map.keys())}",
                }
            # Групуємо слоти по частинах дня: ранок / обід / вечір
            period_order = ["ранок", "обід", "вечір"]
            periods_map: dict[str, list[dict]] = {p: [] for p in period_order}
            for s in matched_slots:
                periods_map[s["period"]].append(s)

            # Будуємо текстовий опис частин дня
            # Якщо в частині ≥ THRESHOLD слотів — показуємо кількість і діапазон (не перелічуємо)
            # Якщо < THRESHOLD — перелічуємо конкретні часи
            period_parts = []
            for pname in period_order:
                pslots = periods_map[pname]
                if not pslots:
                    continue
                if len(pslots) >= _PERIOD_THRESHOLD:
                    period_parts.append(
                        f"{pname}: {len(pslots)} вікон ({pslots[0]['time']}–{pslots[-1]['time']})"
                    )
                else:
                    times = ", ".join(s["time"] for s in pslots)
                    period_parts.append(f"{pname}: {times}")

            periods_text = "; ".join(period_parts)
            active_periods = [p for p in period_order if periods_map[p]]

            # Pre-обчислюємо як ТОЧНО озвучити кожну частину дня.
            # ≥ THRESHOLD слотів → діапазон ("з 8:30 по 11:45")
            # < THRESHOLD слотів → перелік ("8:30, 9:00, 9:30")
            # AI отримує готовий текст — не думає сама, просто зачитує.
            period_say: dict[str, str] = {}
            for p in active_periods:
                pslots = periods_map[p]
                if len(pslots) >= _PERIOD_THRESHOLD:
                    period_say[p] = f"з {pslots[0]['time']} по {pslots[-1]['time']}"
                else:
                    period_say[p] = ", ".join(s["time"] for s in pslots)

            if len(active_periods) == 1:
                # Єдина частина дня — одразу кажемо часи без питання про частину
                only = active_periods[0]
                period_prompt = (
                    f"Скажи пацієнту: 'є час {period_say[only]}' і запитай який зручніше. "
                    f"Тільки числа HH:MM, без словесного формату ('о другій' тощо)."
                )
            else:
                # Кілька частин — спочатку питаємо яка частина, потім кажемо готове формулювання
                guide = "; ".join(f"{p} → {period_say[p]}" for p in active_periods)
                period_prompt = (
                    f"Запитай пацієнта яка частина дня зручніше ({' / '.join(active_periods)}). "
                    f"Коли пацієнт обере — скажи ТІЛЬКИ: {guide}. "
                    f"Тільки числа HH:MM, без словесного формату."
                )

            _matched_weekday = _dates_weekday.get(matched_date_key, "")
            _date_with_weekday = f"{_matched_weekday} {requested_date}" if _matched_weekday else requested_date
            logger.info(f"[TOOL] get_available_slots(date={requested_date}) → {len(matched_slots)} слотів")
            result = {
                "success": True,
                "message": (
                    f"На {_date_with_weekday} є {len(matched_slots)} вільних вікон: {periods_text}. "
                    f"Якщо пацієнт хоче час якого немає в списку — скажи що такого немає і назви найближчий. "
                    f"{period_prompt}"
                ),
                "date": requested_date,
                "weekday": _matched_weekday,
                "slots": matched_slots,
                "total_slots": len(matched_slots),
            }
            # Кешуємо з ключем що включає дату
            call_cache[f"get_available_slots:{requested_date}"] = {"result": result, "_ts": time.monotonic()}
            return result

        # КРОК 1: без дати → повертаємо тільки список дат (БЕЗ часових діапазонів!)
        # НЕ передаємо time_range — AI округлює діапазони і вводить пацієнта в оману
        # (каже "з 9 до 13" а реально 9:20–12:30, або "на 9" а є тільки 9:20).
        # Точні часи покажемо в Кроці 2 коли пацієнт обере дату.
        date_summaries = []
        for d, slots in list(dates_map.items())[:3]:
            date_summaries.append({
                "date": d,
                "weekday": _dates_weekday.get(d, ""),
                "count": len(slots),
            })
        dates_list = ", ".join(f"{d['weekday']} {d['date']}" for d in date_summaries)
        # Блок "той самий день" — ін'єктується в результат залежно від наявності дня запису
        _vd_ordinal = f"{_ua_ordinal(visit_dt.day)} {_MONTHS_UA[visit_dt.month - 1]}"
        _vd_short = f"{visit_dt.day} {_MONTHS_UA[visit_dt.month - 1]}"
        _has_same_day = any(d["date"] == _vd_ordinal for d in date_summaries)
        logger.info(
            f"[TOOL] get_available_slots → {len(raw)} слотів на {len(date_summaries)} дат: {dates_list}"
        )
        natural = _natural_date_list_with_weekday(date_summaries)
        # Жорстка таблиця відповідності дата→день тижня для передачі в AI
        weekday_table = "; ".join(
            f"{d['date']} = {d['weekday']}" for d in date_summaries if d.get("weekday")
        )
        if _has_same_day:
            _same_day_block = (
                f"\n\n⚠ ОСОБЛИВИЙ ВИПАДОК: серед дат є день запису пацієнта ({_vd_ordinal}).\n"
                f"ЯКЩО ЗАПІЗНЮЄТЬСЯ (прийде пізніше, але не хоче змінювати запис):\n"
                f"  → 'Можу перенести запис на пізніший час, або з'єднати з менеджером. Що зручніше?'\n"
                f"  → Перенос → get_available_slots(date='{_vd_short}') → дій за результатом\n"
                f"  → Менеджер → transfer_to_operator(reason='пацієнт запізнюється') → end_call\n"
                f"  → Відмова від обох → confirm_visit() → end_call(confirmed)\n"
                f"ЯКЩО ХОЧЕ ІНШИЙ ЧАС ТОГО Ж ДНЯ ({_vd_ordinal}):\n"
                f"  → ОДРАЗУ get_available_slots(date='{_vd_short}') → дій за результатом"
            )
        else:
            _same_day_block = (
                f"\n\n⚠ ЯКЩО ПАЦІЄНТ ЗАПІЗНЮЄТЬСЯ АБО ХОЧЕ ТОЙ САМИЙ ДЕНЬ ({_vd_ordinal}):\n"
                f"  На {_vd_ordinal} вільних місць немає серед доступних дат.\n"
                f"  ЗАПІЗНЮЄТЬСЯ → 'Щоб повідомити клініку, потрібно з'єднати з менеджером. Бажаєте?'\n"
                f"    → Так → transfer_to_operator(reason='пацієнт запізнюється') → end_call\n"
                f"    → Ні → confirm_visit() → end_call(confirmed)\n"
                f"  ХОЧЕ ТОЙ САМИЙ ДЕНЬ → 'На жаль, на {_vd_ordinal} вільного часу вже немає. Але є {natural}. Бажаєте перенести на інший день?'"
            )

        result = {
            "success": True,
            "message": (
                f"=== СЦЕНАРІЙ ПЕРЕНОСУ/ВІДМОВИ — ДОТРИМУЙСЯ ТОЧНО ===\n\n"
                f"КРОК 1. Скажи пацієнту ДОСЛІВНО: «{natural}. На яку дату зручніше?»\n"
                f"⚠ РІВНО {len(date_summaries)} дат(и) — назви ВСІ, жодну не пропускай і не додавай.\n"
                f"НЕ згадуй конкретні часи зараз — тільки дати.\n"
                f"ТАБЛИЦЯ ДНІВ ТИЖНЯ (100% точна — НЕ рахуй самостійно): {weekday_table}.\n\n"
                f"КРОК 2. Пацієнт назвав дату:\n"
                f"→ Підтверди: 'На [дата], правильно?' Тільки після 'так' → КРОК 3.\n"
                f"→ Якщо дата не з переліку — скажи 'Цієї дати немає. Є: [перелік]' і перепитай.\n"
                f"→ Нечітко/не зрозуміло → перепитай: 'Яку дату маєте на увазі? Є {natural}'\n\n"
                f"КРОК 3. Після підтвердження дати → МОВЧКИ виклич get_available_slots(date='[дата]').\n"
                f"Результат скаже які часи є. Потім:\n"
                f"→ Запитай фінальне підтвердження: 'Переносимо на [дата] о [час]. Все вірно?'\n"
                f"→ 'так/вірно/давай' → reschedule_visit(visit_id, slot_id) → 'Чудово! Запис перенесено. На все добре!' → end_call(rescheduled)\n"
                f"→ 'ні' + хоче іншу дату → повернись до КРОК 2\n\n"
                f"ЯКЩО ПАЦІЄНТ ВІДМОВЛЯЄТЬСЯ ВІД ПЕРЕНОСУ:\n"
                f"→ 'скасуйте/не хочу/відміняйте' → 'Добре, скасовуємо. До побачення!' → cancel_visit() → end_call(cancelled)\n"
                f"→ ОДНЕ 'ні' без 'скасуйте' → перепитай: 'Тобто скасуємо запис чи Ви плануєте прийти?'\n"
                f"  - 'прийду/так/залишаємо' → confirm_visit() → end_call(confirmed)\n"
                f"  - 'скасуйте' → cancel_visit() → end_call(cancelled)\n"
                f"→ 'прийду сьогодні/як планував/нікуди не переношу' → перепитай: 'Тобто Ваш запис залишається в силі?'\n"
                f"  - 'так' → confirm_visit() → end_call(confirmed)\n\n"
                f"ЗАБОРОНЕНО: пропонувати дати яких немає в переліку. Пропонувати скасування першою. Питати 'бажаєте перенести?' більше одного разу."
                + _same_day_block
            ),
            "available_dates": date_summaries,
            "total_dates": len(date_summaries),
        }
        call_cache["get_available_slots"] = {"result": result, "_ts": time.monotonic()}
        return result

    if function_name == "reschedule_visit":
        slot_id = args.get("slot_id", "")
        try:
            int(slot_id)
        except (ValueError, TypeError):
            logger.error("[TOOL] reschedule_visit: невалідний slot_id=%r — AI не викликав get_available_slots(date=...)", slot_id)
            return {
                "success": False,
                "message": (
                    "ПОМИЛКА: slot_id невалідний — ти не викликала get_available_slots(date=...) для цієї дати. "
                    "НЕГАЙНО виклич get_available_slots(date='[дата яку обрав пацієнт]') "
                    "і використай реальний slot_id з отриманого списку slots[]."
                ),
            }
        success = await clinic_api.reschedule_visit(
            visit_id=args["visit_id"],
            slot_id=slot_id,
        )
        result = {
            "success": success,
            "message": (
                "Візит перенесено. Тепер ОДРАЗУ виклич end_call(result='rescheduled'). НЕ питай нічого більше."
                if success else
                "Помилка переносу. Скажи пацієнту і заверши дзвінок."
            ),
        }
        call_cache["reschedule_visit"] = result
        return result

    if function_name == "cancel_visit":
        success = await clinic_api.cancel_visit(
            visit_id=args["visit_id"],
            reason=args.get("reason"),
        )
        result = {
            "success": success,
            "message": (
                "Візит скасовано. Тепер ОДРАЗУ виклич end_call(result='cancelled'). НЕ питай нічого більше."
                if success else
                "Помилка скасування. Скажи пацієнту і заверши дзвінок."
            ),
        }
        call_cache["cancel_visit"] = result
        return result

    if function_name == "end_call":
        result = {"success": True, "result": args.get("result", "no_result")}
        call_cache["end_call"] = result
        return result

    if function_name == "handle_record_error":
        result = {
            "success": True,
            "message": (
                "Скажи пацієнту ДОСЛІВНО:\n"
                "'Розуміємо, вибачте за незручності. Можу з'єднати вас з менеджером для уточнення, або скасувати запис.'\n\n"
                "→ Пацієнт обирає менеджера → 'Добре, перенаправляю. До побачення!' → transfer_to_operator(reason='помилка в записі')\n"
                "→ Пацієнт обирає скасування → cancel_visit() → 'Добре, скасовуємо. На все добре!' → end_call(cancelled)\n"
                "→ Вагається або нечітко → перепитай: 'Що вам зручніше — з'єднати з менеджером, чи скасувати запис?'\n"
                "  → Менеджер → transfer_to_operator(reason='помилка в записі')\n"
                "  → Скасування → cancel_visit() → end_call(cancelled)\n"
                "  → Знову нечітко → дій за ПРАВИЛОМ ЧОТИРЬОХ НЕРОЗУМІНЬ"
            ),
        }
        call_cache["handle_record_error"] = result
        return result

    if function_name == "get_preparation_info":
        from services.sheets_loader import search_service
        _booking_note = (
            "\n\nЯКЩО ПАЦІЄНТ ХОЧЕ ЗАПИСАТИСЬ (до цього або іншого лікаря):\n"
            "'На жаль, записати на іншу процедуру я не можу — це через менеджера. Бажаєте з'єднати?'\n"
            "→ МОВЧКИ ЧЕКАЙ. Тільки після явного 'так' → transfer_to_operator(reason='запис на іншу процедуру').\n"
            "→ Відмова → 'Тоді повернімось до вашого запису. Все в силі?'"
        )
        query = args.get("service_name", "")
        matches = search_service(query)
        if not matches:
            return {
                "found": False,
                "message": (
                    "Інформації про підготовку до цієї процедури немає. "
                    "Скажи пацієнту: 'На жаль, детальної інформації про підготовку до цієї процедури у мене немає. "
                    "Можу з'єднати з менеджером, або можете уточнити на рецепції при візиті.' "
                    "НЕ вигадуй підготовку самостійно."
                    + _booking_note
                ),
            }

        logger.info("[TOOL] get_preparation_info(%r) → %d збігів", query, len(matches))
        for m in matches:
            logger.info("[TOOL]   • рядок=%s | %s | аналог: %s | %s", m.get("row_num", "?"), m["official_name"], m["analog_name"], m["preparation"][:120])

        query_words = [w.lower() for w in query.split() if len(w) > 2]
        exact_matches = [
            m for m in matches
            if any(qw in m["official_name"].lower() or qw in m["analog_name"].lower() for qw in query_words)
        ]
        best = exact_matches or matches

        parts = []
        for m in best:
            label = m["analog_name"] or m["official_name"]
            line = f"• {label}"
            if m["preparation"]:
                line += f"\n  Підготовка: {m['preparation']}"
            parts.append(line)

        if exact_matches:
            return {
                "found": True,
                "query": query,
                "message": (
                    f"Знайдено за запитом '{query}':\n\n"
                    + "\n".join(parts)
                    + "\n\nРозкажи пацієнту цю інформацію природньою мовою (2-3 речення). "
                    "Після пояснення: 'Ви плануєте прийти чи є ще питання?'"
                    + _booking_note
                ),
                "results": exact_matches,
            }
        else:
            alt_names = ", ".join((m["analog_name"] or m["official_name"]) for m in matches[:2])
            return {
                "found": False,
                "query": query,
                "message": (
                    f"Послугу '{query}' не знайдено точно в каталозі. "
                    f"Схожі: {alt_names}. "
                    "Скажи пацієнту що точної інформації немає, але можеш з'єднати з менеджером."
                    + _booking_note
                ),
                "results": [],
            }

    if function_name == "transfer_to_operator":
        reason = args.get("reason", "")
        logger.info(f"[TOOL] transfer_to_operator → {reason}")
        result = {"success": True, "reason": reason}
        call_cache["transfer_to_operator"] = result
        return result

    logger.error(f"[TOOL] Невідома функція: {function_name}")
    return {"error": f"Невідома функція: {function_name}"}
