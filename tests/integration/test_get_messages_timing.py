#!/usr/bin/env python3
"""Integration test for get_messages via MCP Client (higher order function).

Run with: uv run python3 tests/integration/test_get_messages_timing.py
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports.stdio import StdioTransport  # noqa: E402


async def main():
    server_path = str(ROOT / "src" / "server.py")

    # Start server process
    proc = subprocess.Popen(
        [sys.executable, server_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
    )

    await asyncio.sleep(2)

    try:
        print("Connecting to server via stdio...")
        transport = StdioTransport(
            command=sys.executable,
            args=[server_path],
            cwd=str(ROOT),
        )
        mcp = Client(transport)

        async with mcp:
            # Use Saved Messages unless an operator deliberately supplies a test chat.
            print("\n--- Testing get_messages with min_date ---")
            chat_id = os.environ.get("FAST_MCP_TELEGRAM_TEST_CHAT_ID", "me")
            since = os.environ.get("FAST_MCP_TELEGRAM_TEST_MIN_DATE", "2024-01-01")
            limit = 1000

            print(f"Calling get_messages with chat_id={chat_id}, min_date={since}, limit={limit}")
            start = time.time()
            print(f"Starting at {datetime.now().isoformat()}")

            result = await mcp.call_tool(
                "get_messages",
                {"chat_id": chat_id, "min_date": since, "limit": limit},
            )
            elapsed = time.time() - start

            print(f"\nCompleted in {elapsed:.2f}s")
            print(f"Result type: {type(result)}")

            content = result.content[0].text
            resp_data = json.loads(content)
            print(f"Keys: {list(resp_data.keys())}")

            if "messages" in resp_data:
                msgs = resp_data["messages"]
                print(f"Messages count: {len(msgs)}")
                if msgs:
                    print(f"First: [{msgs[0].get('id')}] {msgs[0].get('date')}")
                    print(f"Last: [{msgs[-1].get('id')}] {msgs[-1].get('date')}")
            if "error" in resp_data:
                print(f"ERROR: {resp_data['error']}")

    finally:
        proc.terminate()
        proc.wait(timeout=5)

    print(f"\n{'='*60}")
    print("MCP timing test completed")
    print('='*60)


if __name__ == "__main__":
    def timeout_handler(signum, frame):
        print("\n[TIMEOUT] Test exceeded 30 seconds, exiting...")
        sys.exit(1)

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(30)

    asyncio.run(main())
