"""
Модуль 2.2 — Email звітність.

Щотижневий: кожен понеділок — кількість дзвінків, середні оцінки, топ порушень.
Щомісячний: 1-го числа — рейтинг операторів, найгірші критерії, рекомендації.

Читає дані з Google Sheets (аркуш Результати).
Відправляє HTML листи через SMTP.
"""

import asyncio
import logging
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    B2_CREDENTIALS_PATH,
    B2_RESULT_SHEET_NAME,
    B2_RESULT_SHEETS_ID,
    B3_REPORT_EMAILS,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)

logger = logging.getLogger(__name__)

_MONTHS_UA = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

_FIXED_COLS = {
    "ID дзвінка", "Дата/час", "Лінія", "Оператор", "Напрям",
    "Тривалість (с)", "Оцінка (%)", "AI коментар", "Маркери (є)", "Маркери (немає)",
}


class EmailReporter:

    def __init__(self):
        self._service = None

    # ------------------------------------------------------------------
    # Читання Sheets
    # ------------------------------------------------------------------

    def _get_service(self):
        if self._service:
            return self._service
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            B2_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def _read_sheet_sync(self) -> list[list]:
        svc = self._get_service()
        res = (
            svc.spreadsheets().values()
            .get(spreadsheetId=B2_RESULT_SHEETS_ID, range=f"{B2_RESULT_SHEET_NAME}!A1:ZZ")
            .execute()
        )
        return res.get("values", [])

    async def _read_rows(self) -> list[dict]:
        rows = await asyncio.to_thread(self._read_sheet_sync)
        if not rows or len(rows) < 2:
            return []
        headers = rows[0]
        result = []
        for row in rows[1:]:
            padded = row + [""] * max(0, len(headers) - len(row))
            result.append(dict(zip(headers, padded)))
        return result

    # ------------------------------------------------------------------
    # Утиліти
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(val: str) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(val.strip()[:len(fmt)], fmt)
            except Exception:
                pass
        return None

    @staticmethod
    def _parse_score(val: str) -> float:
        try:
            return float(val.replace("%", "").strip()) / 100
        except Exception:
            return 0.0

    async def _rows_in_range(self, since: datetime, until: datetime) -> list[dict]:
        all_rows = await self._read_rows()
        result = []
        for r in all_rows:
            dt = self._parse_dt(r.get("Дата/час", ""))
            if dt and since <= dt < until:
                result.append(r)
        return result

    # ------------------------------------------------------------------
    # Щотижневий звіт
    # ------------------------------------------------------------------

    async def send_weekly_report(self, since: datetime, until: datetime) -> None:
        if not self._can_send():
            return
        rows = await self._rows_in_range(since, until)
        logger.info(f"[REPORTER] Тижневий звіт: {len(rows)} дзвінків ({since.date()}–{until.date()})")
        html = self._weekly_html(rows, since, until)
        subject = (
            f"Тижневий звіт колл-центру "
            f"({since.strftime('%d.%m')}–{(until - timedelta(days=1)).strftime('%d.%m.%Y')})"
        )
        await asyncio.to_thread(self._send, subject, html)

    def _weekly_html(self, rows: list[dict], since: datetime, until: datetime) -> str:
        total = len(rows)
        period = f"{since.strftime('%d.%m.%Y')} – {(until - timedelta(days=1)).strftime('%d.%m.%Y')}"

        if not total:
            return f"<p>За тиждень {period} дзвінків не знайдено.</p>"

        scores = [self._parse_score(r.get("Оцінка (%)", "0")) for r in rows]
        avg = sum(scores) / len(scores)

        # Оцінки по операторах
        op_scores: dict[str, list[float]] = defaultdict(list)
        for r, s in zip(rows, scores):
            op_scores[r.get("Оператор", "?") or "?"].append(s)

        # Топ порушень маркерів
        violations: dict[str, int] = defaultdict(int)
        for r in rows:
            bad = r.get("Маркери (немає)", "") or ""
            if bad and bad != "—":
                for v in bad.split(","):
                    v = v.strip()
                    if v:
                        violations[v] += 1
        top_viol = sorted(violations.items(), key=lambda x: -x[1])[:5]

        op_rows_html = ""
        for op, sc_list in sorted(op_scores.items()):
            op_avg = sum(sc_list) / len(sc_list)
            color = self._score_color(op_avg)
            op_rows_html += (
                f"<tr>"
                f"<td style='padding:8px;border:1px solid #ddd'>{op}</td>"
                f"<td style='padding:8px;border:1px solid #ddd;text-align:center'>{len(sc_list)}</td>"
                f"<td style='padding:8px;border:1px solid #ddd;text-align:center;"
                f"color:{color};font-weight:bold'>{op_avg * 100:.1f}%</td>"
                f"</tr>"
            )

        viol_html = "".join(f"<li><b>{p}</b> — {n} раз(и)</li>" for p, n in top_viol)
        if not viol_html:
            viol_html = "<li>Порушень не виявлено</li>"

        return f"""
<html><body style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;color:#2c3e50">
<h2>📊 Тижневий звіт колл-центру Медіком</h2>
<p style="color:#7f8c8d">{period}</p>
<table style="width:100%;border-collapse:collapse;margin:12px 0">
  <tr style="background:#ecf0f1">
    <td style="padding:10px;border:1px solid #bdc3c7"><b>Всього дзвінків</b></td>
    <td style="padding:10px;border:1px solid #bdc3c7;font-size:22px"><b>{total}</b></td>
  </tr>
  <tr>
    <td style="padding:10px;border:1px solid #bdc3c7"><b>Середня оцінка</b></td>
    <td style="padding:10px;border:1px solid #bdc3c7;font-size:22px;
       color:{self._score_color(avg)};font-weight:bold">{avg * 100:.1f}%</td>
  </tr>
</table>
<h3>Оператори</h3>
<table style="width:100%;border-collapse:collapse">
  <tr style="background:#3498db;color:white">
    <th style="padding:8px;text-align:left">Оператор</th>
    <th style="padding:8px">Дзвінків</th>
    <th style="padding:8px">Середня оцінка</th>
  </tr>
  {op_rows_html}
</table>
<h3>Топ порушень фраз-маркерів</h3>
<ul>{viol_html}</ul>
<hr style="border:none;border-top:1px solid #ecf0f1;margin:20px 0">
<p style="color:#95a5a6;font-size:12px">Звіт сформовано автоматично · Медіком AI</p>
</body></html>
"""

    # ------------------------------------------------------------------
    # Щомісячний звіт
    # ------------------------------------------------------------------

    async def send_monthly_report(self, year: int, month: int) -> None:
        if not self._can_send():
            return
        since = datetime(year, month, 1)
        until = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        rows = await self._rows_in_range(since, until)
        logger.info(f"[REPORTER] Місячний звіт {year}-{month:02d}: {len(rows)} дзвінків")
        html = self._monthly_html(rows, since)
        subject = f"Місячний звіт колл-центру — {_MONTHS_UA[month]} {year}"
        await asyncio.to_thread(self._send, subject, html)

    def _monthly_html(self, rows: list[dict], since: datetime) -> str:
        total = len(rows)
        month_label = f"{_MONTHS_UA[since.month]} {since.year}"

        if not total:
            return f"<p>За {month_label} дзвінків не знайдено.</p>"

        scores = [self._parse_score(r.get("Оцінка (%)", "0")) for r in rows]
        avg = sum(scores) / len(scores)

        # Дані по операторах
        op_data: dict[str, dict] = {}
        for r, s in zip(rows, scores):
            op = r.get("Оператор", "?") or "?"
            if op not in op_data:
                op_data[op] = {"scores": [], "violations": defaultdict(int)}
            op_data[op]["scores"].append(s)
            bad = r.get("Маркери (немає)", "") or ""
            if bad and bad != "—":
                for v in bad.split(","):
                    v = v.strip()
                    if v:
                        op_data[op]["violations"][v] += 1

        # Рейтинг операторів (за середньою оцінкою)
        op_ranked = sorted(
            op_data.items(),
            key=lambda x: sum(x[1]["scores"]) / len(x[1]["scores"]),
            reverse=True,
        )

        # Найгірші критерії
        criteria_keys = [k for k in (rows[0].keys() if rows else []) if k not in _FIXED_COLS]
        criteria_fail: dict[str, int] = {}
        for k in criteria_keys:
            criteria_fail[k] = sum(1 for r in rows if r.get(k, "✓") == "✗")
        worst_criteria = sorted(criteria_fail.items(), key=lambda x: -x[1])[:5]

        # Рекомендації
        recommendations = []
        for criterion, cnt in worst_criteria:
            pct = cnt / total * 100
            if pct > 25:
                recommendations.append(
                    f"Навчання з теми «{criterion}» (порушення у {pct:.0f}% дзвінків)"
                )

        medals = ["🥇", "🥈", "🥉"]
        op_rows_html = ""
        for i, (op, d) in enumerate(op_ranked):
            op_avg = sum(d["scores"]) / len(d["scores"])
            color = self._score_color(op_avg)
            badge = medals[i] if i < 3 else f"{i + 1}."
            op_rows_html += (
                f"<tr>"
                f"<td style='padding:8px;border:1px solid #ddd'>{badge} {op}</td>"
                f"<td style='padding:8px;border:1px solid #ddd;text-align:center'>{len(d['scores'])}</td>"
                f"<td style='padding:8px;border:1px solid #ddd;text-align:center;"
                f"color:{color};font-weight:bold'>{op_avg * 100:.1f}%</td>"
                f"</tr>"
            )

        worst_html = "".join(
            f"<li><b>{c}</b> — {n} порушень ({n / total * 100:.0f}%)</li>"
            for c, n in worst_criteria
        ) or "<li>Немає даних</li>"

        rec_html = "".join(f"<li>{r}</li>" for r in recommendations)
        if not rec_html:
            rec_html = "<li>Всі показники в нормі — навчання не потребується</li>"

        return f"""
<html><body style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;color:#2c3e50">
<h2>📊 Місячний звіт колл-центру Медіком — {month_label}</h2>
<table style="width:100%;border-collapse:collapse;margin:12px 0">
  <tr style="background:#ecf0f1">
    <td style="padding:10px;border:1px solid #bdc3c7"><b>Всього дзвінків</b></td>
    <td style="padding:10px;border:1px solid #bdc3c7;font-size:22px"><b>{total}</b></td>
  </tr>
  <tr>
    <td style="padding:10px;border:1px solid #bdc3c7"><b>Середня оцінка за місяць</b></td>
    <td style="padding:10px;border:1px solid #bdc3c7;font-size:22px;
       color:{self._score_color(avg)};font-weight:bold">{avg * 100:.1f}%</td>
  </tr>
</table>
<h3>🏆 Рейтинг операторів</h3>
<table style="width:100%;border-collapse:collapse">
  <tr style="background:#3498db;color:white">
    <th style="padding:8px;text-align:left">Оператор</th>
    <th style="padding:8px">Дзвінків</th>
    <th style="padding:8px">Середня оцінка</th>
  </tr>
  {op_rows_html}
</table>
<h3>⚠️ Найчастіші порушення критеріїв</h3>
<ul>{worst_html}</ul>
<h3>📚 Рекомендації щодо навчання</h3>
<ul>{rec_html}</ul>
<hr style="border:none;border-top:1px solid #ecf0f1;margin:20px 0">
<p style="color:#95a5a6;font-size:12px">Звіт сформовано автоматично · Медіком AI</p>
</body></html>
"""

    # ------------------------------------------------------------------
    # SMTP
    # ------------------------------------------------------------------

    def _can_send(self) -> bool:
        if not SMTP_HOST:
            logger.warning("[REPORTER] SMTP_HOST не задано — email вимкнено")
            return False
        if not B3_REPORT_EMAILS:
            logger.warning("[REPORTER] B3_REPORT_EMAILS не задано — нікому надсилати")
            return False
        if not B2_RESULT_SHEETS_ID:
            logger.warning("[REPORTER] B2_RESULT_SHEETS_ID не задано — немає звідки читати дані")
            return False
        return True

    def _send(self, subject: str, html: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM or SMTP_USER
        msg["To"]      = ", ".join(B3_REPORT_EMAILS)
        msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.sendmail(msg["From"], B3_REPORT_EMAILS, msg.as_string())
            logger.info(f"[REPORTER] Надіслано: «{subject}» → {B3_REPORT_EMAILS}")
        except Exception as e:
            logger.error(f"[REPORTER] Помилка відправки: {e!r}")

    @staticmethod
    def _score_color(score: float) -> str:
        if score >= 0.8:
            return "#2d8a4e"
        if score >= 0.6:
            return "#e67e22"
        return "#c0392b"
