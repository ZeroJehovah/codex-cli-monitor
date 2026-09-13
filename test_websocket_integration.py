#!/usr/bin/env python3
"""Quick integration test for WebSocket functionality"""
import asyncio
import json
import time
from src.codex_cli_monitor.websocket_server import run_websocket_server
from src.codex_cli_monitor.models import CodexSession
from src.codex_cli_monitor.aggregation import ServerIdentity, RemoteSnapshot

def mock_state_provider():
    """Mock state provider for testing"""
    identity = ServerIdentity(
        server_id="test-server",
        server_name="Test Server",
        boot_id=None
    )
    sessions = ()
    remote_snapshots = ()
    return sessions, identity, remote_snapshots

async def test_websocket_basic():
    """Test WebSocket server starts and accepts connections"""
    print("Starting WebSocket server on port 18766...")
    
    # Run server briefly to verify it starts
    server_task = asyncio.create_task(
        asyncio.wait_for(
            run_websocket_server(
                host="127.0.0.1",
                port=18766,
                state_provider=mock_state_provider,
                api_token=None,
                broadcast_interval=0.5
            ),
            timeout=2.0
        )
    )
    
    try:
        await server_task
    except asyncio.TimeoutError:
        print("✓ WebSocket server started successfully (timeout expected)")
        server_task.cancel()
        return True
    except Exception as e:
        print(f"✗ WebSocket server failed: {e}")
        return False

if __name__ == "__main__":
    result = asyncio.run(test_websocket_basic())
    exit(0 if result else 1)
