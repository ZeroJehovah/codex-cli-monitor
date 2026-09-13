#!/usr/bin/env python3
"""Test WebSocket upgrade endpoint on HTTP server."""

from aiohttp import web
import asyncio
import aiohttp

routes = web.RouteTableDef()

@routes.get('/ws')
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # Send initial message
    await ws.send_str('{"test": "websocket working"}')
    
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close':
                await ws.close()
            else:
                await ws.send_str(f'echo: {msg.data}')
    
    return ws

async def main():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 9999)
    await site.start()
    print("Test server started on http://127.0.0.1:9999/ws")
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
