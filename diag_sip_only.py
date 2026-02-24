"""
SIP diagnostic: accept INVITE (expecting_call=True), see full flow.
"""
import asyncio
import os
import json
import logging

os.environ["ENV"] = "production"

_OUTFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diag_output.txt")
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sip_debug.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8", mode="w"),
    ],
)


async def main():
    from config import (
        BINOTEL_SIP_SERVER, BINOTEL_SIP_PORT,
        BINOTEL_SIP_USERNAME, BINOTEL_SIP_PASSWORD,
    )
    from services.binotel_sip import BinotelSipClient
    from services.binotel_rest import BinotelRestClient

    out = open(_OUTFILE, "w", encoding="utf-8")

    def p(*args):
        line = " ".join(str(a) for a in args)
        out.write(line + "\n")
        out.flush()

    rest = BinotelRestClient()

    p("[1] Starting SIP client...")
    sip = BinotelSipClient(
        server=BINOTEL_SIP_SERVER,
        port=BINOTEL_SIP_PORT,
        username=BINOTEL_SIP_USERNAME,
        password=BINOTEL_SIP_PASSWORD,
    )
    sip.start()
    p("[1] SIP started. Waiting 8s for initial Binotel qualify ping...")
    await asyncio.sleep(8)

    p("[2] Checking extension 914 status...")
    result = await rest.send_request("settings/list-of-employees", {})
    for k, emp in result.get("listOfEmployees", {}).items():
        ep = emp.get("endpointData", {})
        if isinstance(ep, dict) and str(ep.get("internalNumber", "")) == "914":
            status = ep.get("status", {})
            st = status.get("status", "") if isinstance(status, dict) else str(status)
            qualify = status.get("qualify", "?") if isinstance(status, dict) else "?"
            p(f"  ext 914: status='{st}', qualify={qualify}")

    p("")
    p("[3] Setting expecting_call=True and starting INVITE listener task...")
    sip._expecting_call = True
    sip_task = asyncio.create_task(sip.wait_for_call(timeout=50))

    p("[4] Calling initiate_call REST API (will wait for REST API response)...")
    try:
        result2 = await rest.send_request(
            "calls/internal-number-to-external-number",
            {
                "internalNumber": "914",
                "externalNumber": "0687034410",
                "limitCallTime": "60",
            },
        )
        p("REST API Response:", json.dumps(result2, ensure_ascii=False, indent=2))
    except Exception as e:
        p(f"REST API Exception: {e}")
        sip_task.cancel()
        sip.stop()
        out.close()
        return

    p("")
    p("[5] Waiting for SIP INVITE result...")
    sip_audio = await sip_task
    if sip_audio is None:
        p("  No INVITE received (timeout)")
    else:
        p("  INVITE received and answered!")
        p("  is_active:", sip_audio.is_active)
        p("  Waiting 5s for audio...")
        await asyncio.sleep(5)
        p("  Hanging up...")
        sip_audio.hangup()

    p("[6] Stopping SIP...")
    sip.stop()
    p("[7] Done. Check sip_debug.log for details.")
    out.close()


asyncio.run(main())
