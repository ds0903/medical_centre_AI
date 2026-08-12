"""
Одноразовий тест stats/call-record — пробує різні параметри.
Запуск: python test_call_record.py
"""
import asyncio
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()


async def main():
    from services.binotel_rest import BinotelRestClient
    from datetime import datetime, timedelta, timezone

    client = BinotelRestClient()

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=2)

    print(f"Запит за {since.strftime('%H:%M')}–{now.strftime('%H:%M')} UTC")
    calls = await client.get_calls_for_period(
        since.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    )

    with_rec = [c for c in calls if c.get("recordingStatus") == "uploaded"]
    print(f"Дзвінків: {len(calls)}, з записом: {len(with_rec)}")

    if not with_rec:
        print("Немає дзвінків з записом за цей час — спробуйте збільшити проміжок")
        return

    # Показати поля першого дзвінка
    sample = with_rec[0]
    print(f"\n--- Поля першого дзвінка ---")
    for k, v in sample.items():
        print(f"  {k}: {v}")

    # Спробувати різні параметри для stats/call-record
    print(f"\n--- Тест stats/call-record (перші 3 дзвінки) ---")
    for c in with_rec[:3]:
        call_id     = str(c.get("callID", ""))
        general_id  = str(c.get("generalCallID", ""))
        print(f"\n  generalCallID={general_id}  callID={call_id}")

        test_params = [
            ("callID",         call_id),
            ("generalCallID",  general_id),
        ]
        if call_id != general_id:
            # якщо є sub-call IDs у historyData
            for h in (c.get("historyData") or [])[:2]:
                h_id = str(h.get("callID", ""))
                if h_id and h_id not in (call_id, general_id):
                    test_params.append(("callID", h_id))

        for param_name, val in test_params:
            if not val:
                continue
            result = await client.send_request("stats/call-record", {param_name: val})
            status = result.get("status")
            if status == "success":
                print(f"    ✅ {param_name}={val} → {result}")
            else:
                code = result.get("code")
                msg  = result.get("message", "")
                print(f"    ❌ {param_name}={val} → code={code} '{msg}'")


if __name__ == "__main__":
    asyncio.run(main())
