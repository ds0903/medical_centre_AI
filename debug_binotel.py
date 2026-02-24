"""Діагностика: що бачить Binotel про наш extension."""
import asyncio, os, json
os.environ["ENV"] = "production"

async def main():
    from services.binotel_rest import BinotelRestClient
    rest = BinotelRestClient()

    print("\n=== LIST OF EMPLOYEES ===")
    result = await rest.send_request("settings/list-of-employees", {})
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
