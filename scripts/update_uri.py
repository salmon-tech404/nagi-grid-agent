"""Update Nagi's ERC-8004 registration file in place (same agentId), e.g. to add the image.

Usage:
    uv run python scripts/update_uri.py --dry-run   # print the new file, send nothing
    uv run python scripts/update_uri.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx
from bnbagent import ERC8004Agent, EVMWalletProvider

from register import CAMPAIGN_WALLET, build_agent_uri, decode_agent_uri

AGENT_ID = int(os.getenv("AGENT_ID", "2623"))


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    base_url = os.getenv("A2A_BASE_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        sys.exit("A2A_BASE_URL must be the public https URL of the running server")
    image = f"{base_url}/logo.png"
    resp = httpx.head(image, timeout=60, follow_redirects=True)
    if resp.status_code != 200 or "image/png" not in resp.headers.get("content-type", ""):
        sys.exit(f"{image} is not live yet ({resp.status_code} {resp.headers.get('content-type')}); deploy first")

    wallet = EVMWalletProvider(password=os.environ["WALLET_PASSWORD"], private_key=os.environ["PRIVATE_KEY"], persist=False)
    if wallet.address.lower() != CAMPAIGN_WALLET:
        sys.exit(f"wallet {wallet.address} is not the campaign wallet {CAMPAIGN_WALLET}")
    sdk = ERC8004Agent(wallet_provider=wallet, network=os.getenv("NETWORK", "bsc-testnet"))

    uri = build_agent_uri(sdk, base_url, os.getenv("AGENT_NAME", "Nagi"), image=image, agent_id=AGENT_ID)
    print(f"Updating agent {AGENT_ID} from {sdk.wallet_address}")
    print(json.dumps(decode_agent_uri(uri), indent=1))
    if dry_run:
        print("--dry-run: nothing sent")
        return
    result = sdk.set_agent_uri(AGENT_ID, uri)
    print("  tx:", result.get("transactionHash"), "| success:", result.get("success"))


if __name__ == "__main__":
    main()
