"""
Email звітність Модуля 2.1 — щотижнева та щомісячна розсилка.

Стан надсилання зберігається в data/report_state.json:
  {"weekly_last": "2026-08-11", "monthly_last": "2026-08"}
"""
import json
import logging
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent / "data" / "report_state.json"

_RESULT_LABELS = {
    "booked":    "Записано",
    "declined":  "Відмовились",
    "self_book": "Самостійно запишуться",
    "no_answer": "Не додзвонились",
    "no_result": "Без результату",
}

_DECLINE_LABELS = {
    1:  "Не вказали причину",
    2:  "Не встигають доїхати",
    3:  "Змінилися плани",
    4:  "Викликали на роботу",
    5:  "Їде у відрядження",
    6:  "Одужання",
    7:  "Хотіли швидше",
    8:  "Були в іншого фахівця",
    9:  "Дорого",
    10: "Захворіла дитина",
    11: "Вже не треба",
    12: "Потрапили в лікарню",
    13: "Уже були на прийомі",
    14: "Не готові всі відповіді аналізів",
    15: "Записані в інший день",
    16: "Захворіли",
    17: "Поїхали",
    18: "За сімейними обставинами",
    19: "Нема з ким залишити дитину",
    20: "Погодні умови",
    21: "Викликали додому лікаря",
    22: "За станом здоров'я (гін.)",
    23: "Знайшли територіально ближче",
    24: "Транспортні проблеми",
    25: "Смерть пацієнта",
    26: "Повітряна тривога",
}


# ---------------------------------------------------------------------------
# Стан
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Перевірка чи треба надсилати
# ---------------------------------------------------------------------------

def should_send_weekly() -> bool:
    today = date.today()
    if today.weekday() != 0:  # 0 = понеділок
        return False
    state = _load_state()
    return state.get("weekly_last") != today.isoformat()


def should_send_monthly() -> bool:
    today = date.today()
    if today.day != 1:
        return False
    state = _load_state()
    month_key = today.strftime("%Y-%m")
    return state.get("monthly_last") != month_key


# ---------------------------------------------------------------------------
# Запити до БД
# ---------------------------------------------------------------------------

async def _fetch_stats(date_from: datetime, date_to: datetime) -> dict:
    from db.database import get_session
    from db.models import RepeatCallTask, RepeatCallStatus, RepeatCallResult

    async with get_session() as session:
        # Підрахунок по результатах
        rows = await session.execute(
            select(RepeatCallTask.result, func.count().label("cnt"))
            .where(
                RepeatCallTask.completed_at >= date_from,
                RepeatCallTask.completed_at < date_to,
                RepeatCallTask.status.in_([
                    RepeatCallStatus.completed,
                    RepeatCallStatus.failed,
                ]),
            )
            .group_by(RepeatCallTask.result)
        )
        result_counts = {str(r.result.value if r.result else "no_result"): r.cnt for r in rows}

        # Підрахунок категорій відмов
        declined_rows = await session.execute(
            select(RepeatCallTask.decline_category, func.count().label("cnt"))
            .where(
                RepeatCallTask.completed_at >= date_from,
                RepeatCallTask.completed_at < date_to,
                RepeatCallTask.result == RepeatCallResult.declined,
                RepeatCallTask.decline_category.isnot(None),
                RepeatCallTask.decline_category != "",
            )
            .group_by(RepeatCallTask.decline_category)
            .order_by(func.count().desc())
        )
        decline_counts = {r.decline_category: r.cnt for r in declined_rows}

        total = await session.scalar(
            select(func.count())
            .where(
                RepeatCallTask.completed_at >= date_from,
                RepeatCallTask.completed_at < date_to,
                RepeatCallTask.status.in_([
                    RepeatCallStatus.completed,
                    RepeatCallStatus.failed,
                ]),
            )
        ) or 0

    return {
        "total": total,
        "result_counts": result_counts,
        "decline_counts": decline_counts,
    }


# ---------------------------------------------------------------------------
# Форматування тексту листа
# ---------------------------------------------------------------------------

def _fmt_results(result_counts: dict, total: int) -> str:
    lines = []
    for key, label in _RESULT_LABELS.items():
        cnt = result_counts.get(key, 0)
        pct = round(cnt / total * 100) if total else 0
        lines.append(f"  • {label}: {cnt} ({pct}%)")
    return "\n".join(lines)


def _fmt_declines(decline_counts: dict, top: int | None = None) -> str:
    if not decline_counts:
        return "  Немає даних"
    items = sorted(decline_counts.items(), key=lambda x: x[1], reverse=True)
    if top:
        items = items[:top]
    lines = []
    for category, cnt in items:
        lines.append(f"  • {category}: {cnt}")
    return "\n".join(lines)


def _build_weekly_body(stats: dict, period_start: date, period_end: date) -> str:
    total = stats["total"]
    booked = stats["result_counts"].get("booked", 0)
    conversion = round(booked / total * 100) if total else 0
    return f"""Щотижневий звіт — Модуль 2.1 «Повторні візити»
Період: {period_start.strftime('%d.%m.%Y')} – {period_end.strftime('%d.%m.%Y')}

ЗАГАЛЬНА СТАТИСТИКА
  Всього дзвінків: {total}
  Конверсія (записано): {conversion}%

РОЗПОДІЛ ЗА СТАТУСАМИ
{_fmt_results(stats['result_counts'], total)}

ПРИЧИНИ ВІДМОВ
{_fmt_declines(stats['decline_counts'])}
"""


def _build_monthly_body(
    stats: dict,
    prev_stats: dict,
    period_start: date,
    period_end: date,
) -> str:
    total = stats["total"]
    booked = stats["result_counts"].get("booked", 0)
    conversion = round(booked / total * 100) if total else 0

    prev_total = prev_stats["total"]
    prev_booked = prev_stats["result_counts"].get("booked", 0)
    prev_conversion = round(prev_booked / prev_total * 100) if prev_total else 0

    delta_total = total - prev_total
    delta_conv  = conversion - prev_conversion

    return f"""Щомісячний звіт — Модуль 2.1 «Повторні візити»
Період: {period_start.strftime('%d.%m.%Y')} – {period_end.strftime('%d.%m.%Y')}

ЗАГАЛЬНА СТАТИСТИКА
  Всього дзвінків: {total} (зміна: {delta_total:+d} порівняно з минулим місяцем)
  Конверсія (записано): {conversion}% (зміна: {delta_conv:+d}%)

РОЗПОДІЛ ЗА СТАТУСАМИ
{_fmt_results(stats['result_counts'], total)}

ТОП-5 ПРИЧИН ВІДМОВ
{_fmt_declines(stats['decline_counts'], top=5)}

ПОРІВНЯННЯ З МИНУЛИМ МІСЯЦЕМ
  Дзвінків: {prev_total} → {total}
  Записано: {prev_booked} → {booked}
  Конверсія: {prev_conversion}% → {conversion}%
"""


# ---------------------------------------------------------------------------
# Надсилання SMTP
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str) -> None:
    from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, B2_REPORT_EMAILS

    if not SMTP_HOST or not B2_REPORT_EMAILS:
        logger.warning("[REPORT] SMTP не налаштовано або B2_REPORT_EMAILS порожній — пропускаємо")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = ", ".join(B2_REPORT_EMAILS)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.sendmail(SMTP_FROM, B2_REPORT_EMAILS, msg.as_bytes())
        logger.info(f"[REPORT] Надіслано: {subject} → {B2_REPORT_EMAILS}")
    except Exception as e:
        logger.error(f"[REPORT] Помилка надсилання: {e}")


# ---------------------------------------------------------------------------
# Публічні функції
# ---------------------------------------------------------------------------

async def send_weekly_report() -> None:
    today = date.today()
    period_end   = today
    period_start = today - timedelta(days=7)

    date_from = datetime(period_start.year, period_start.month, period_start.day, tzinfo=None)
    date_to   = datetime(period_end.year,   period_end.month,   period_end.day,   tzinfo=None)

    logger.info(f"[REPORT] Тижневий звіт: {period_start} – {period_end}")
    stats = await _fetch_stats(date_from, date_to)
    body  = _build_weekly_body(stats, period_start, period_end)
    subject = f"[Медіком] Тижневий звіт — повторні візити ({today.strftime('%d.%m.%Y')})"
    _send_email(subject, body)

    state = _load_state()
    state["weekly_last"] = today.isoformat()
    _save_state(state)


async def send_monthly_report() -> None:
    today = date.today()
    # Поточний місяць
    period_start = date(today.year, today.month, 1)
    if today.month == 12:
        period_end = date(today.year + 1, 1, 1)
    else:
        period_end = date(today.year, today.month + 1, 1)

    # Попередній місяць
    if today.month == 1:
        prev_start = date(today.year - 1, 12, 1)
        prev_end   = date(today.year, 1, 1)
    else:
        prev_start = date(today.year, today.month - 1, 1)
        prev_end   = period_start

    date_from = datetime(period_start.year, period_start.month, period_start.day)
    date_to   = datetime(period_end.year,   period_end.month,   period_end.day)
    prev_from = datetime(prev_start.year,   prev_start.month,   prev_start.day)
    prev_to   = datetime(prev_end.year,     prev_end.month,     prev_end.day)

    logger.info(f"[REPORT] Місячний звіт: {period_start} – {period_end}")
    stats      = await _fetch_stats(date_from, date_to)
    prev_stats = await _fetch_stats(prev_from, prev_to)
    body    = _build_monthly_body(stats, prev_stats, period_start, period_end - timedelta(days=1))
    subject = f"[Медіком] Місячний звіт — повторні візити ({period_start.strftime('%m.%Y')})"
    _send_email(subject, body)

    state = _load_state()
    state["monthly_last"] = today.strftime("%Y-%m")
    _save_state(state)


async def check_and_send_reports() -> None:
    """Викликається щогодини планувальником — надсилає звіт якщо потрібно."""
    if should_send_weekly():
        await send_weekly_report()
    if should_send_monthly():
        await send_monthly_report()
