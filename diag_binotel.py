"""
Diagnostic: check ext 914 status while pyVoIP is running, then try initiate_call.
Writes output to diag_output.txt
"""
import asyncio
import os
import json
import sys

os.environ["ENV"] = "production"

_OUTFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diag_output.txt")
_LOG = open(_OUTFILE, "w", encoding="utf-8")


def p(*args):
    line = " ".join(str(a) for a in args)
    _LOG.write(line + "\n")
    _LOG.flush()


async def main():
    import services.binotel as binotel
    from services.binotel_rest import BinotelRestClient

    rest = BinotelRestClient()

    p("[1] Starting SIP client...")
    await binotel.startup()
    p("[1] SIP started. Waiting 20s for keep-alive to take effect...")
    await asyncio.sleep(20)

    p("[2] Checking extension 914 status...")
    result = await rest.send_request("settings/list-of-employees", {})
    employees = result.get("listOfEmployees", {})
    ext914_found = False
    for k, emp in employees.items():
        ep = emp.get("endpointData", {})
        if isinstance(ep, dict) and str(ep.get("internalNumber", "")) == "914":
            ext914_found = True
            status = ep.get("status", {})
            st = status.get("status", "") if isinstance(status, dict) else str(status)
            qualify = status.get("qualify", "?") if isinstance(status, dict) else "?"
            p(f"  ext 914 status = '{st}', qualify = {qualify}")
            p(f"  full status = {json.dumps(status, ensure_ascii=False)}")
            p(f"  wasOnlineAt = {ep.get('wasOnlineAt', '?')}")

    if not ext914_found:
        p("  ext 914 NOT FOUND!")

    p("")
    p("[3] Trying initiate_call REST API...")
    try:
        result2 = await rest.send_request(
            "calls/internal-number-to-external-number",
            {
                "internalNumber": "914",
                "externalNumber": "0687034410",
                "limitCallTime": "60",
            },
        )
        p("Response:", json.dumps(result2, ensure_ascii=False, indent=2))
    except Exception as e:
        p(f"Exception: {e}")

    p("")
    p("[4] Waiting 3s for any SIP messages...")
    await asyncio.sleep(3)

    await binotel.shutdown()
    p("[5] Done.")
    _LOG.close()


asyncio.run(main())
