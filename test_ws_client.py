import asyncio
import websockets
import json

async def test_websocket():
    uri = "ws://127.0.0.1:8766"
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to {uri}")
            
            # Receive initial state
            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            data = json.loads(message)
            print(f"Received initial state: {len(message)} bytes")
            print(f"Session count: {len(data.get('sessions', []))}")
            print(f"Server count: {data.get('server_count', 0)}")
            
            # Wait for updates
            print("Waiting for state updates...")
            for i in range(3):
                message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                data = json.loads(message)
                print(f"Update {i+1}: {len(data.get('sessions', []))} sessions")
    except asyncio.TimeoutError:
        print("Timeout waiting for message")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test_websocket())
