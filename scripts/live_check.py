"""Stage 1 live check against the real Discord server, run locally.

Prints message IDs, channel IDs, reasons, counts and timings. It never prints
message text, so nothing real lands in your terminal history.

    DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... DEV_USER_ID=... \
    DIGEST_CONFIG=config.local.toml python -m scripts.live_check [--days 7]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from discord_mcp import audit
from discord_mcp.config import load_config
from discord_mcp.digest import Digest
from discord_mcp.discord_client import DiscordClient
from discord_mcp.settings import load_settings


async def run(days: int) -> None:
    settings = load_settings()
    if settings.dev_user_id is None:
        raise SystemExit("set DEV_USER_ID to your Discord user ID")
    config = load_config(settings.config_path)
    if not config.allowlist:
        raise SystemExit("the allowlist in DIGEST_CONFIG is empty; add your test channel")

    client = DiscordClient(settings.bot_token)
    digest = Digest(client, guild_id=settings.guild_id, config=config)
    try:
        identity = await digest.resolve_identity(settings.dev_user_id)
        print(f"roles held: {len(identity.role_ids)}")

        start = time.monotonic()
        result = await digest.get_my_pings(identity, days=days)
        elapsed = time.monotonic() - start
    finally:
        await client.aclose()

    payload = result.payload
    size = len(json.dumps(payload).encode())
    print(f"channels+threads read: {len(result.channel_ids)}  skipped: {payload['skipped_channels']}")
    print(f"conversations: {payload['counts']}  omitted: {payload['omitted']}  truncated: {payload['truncated']}")
    print(f"time: {elapsed:.1f}s  result size: {size / 1024:.0f} KB")
    print("status       pings  link of latest ping (IDs only)")
    for c in payload["conversations"]:
        print(f"{c['status']:<12} {c.get('ping_count', len(c['pings'])):<6} {c['pings'][-1]['link']}")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    sys.excepthook = audit.safe_excepthook
    audit.configure_logging()
    asyncio.run(run(args.days))


if __name__ == "__main__":
    main()
