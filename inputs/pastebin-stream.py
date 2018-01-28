import asyncio
import websockets
import json
import logging
from datetime import datetime

async def produce(queue):
    while True:
        try:
            async with websockets.connect("wss://pastebin-stream.cryto.net/stream") as websocket:
                logging.info("pastebin-stream reconnected")
                await websocket.send('{"type": "subscribe"}')
                while True:
                    data = await websocket.recv()
                    logging.info("pastebin-stream event received")
                    message = json.loads(data)
                    # logging.debug(message)
                    if message['type'] == 'newPaste':
                        paste_data = {
                            'pasteid': message['data']['id'],
                            'pastesite': 'pastebin-stream',
                            'scrape_url': message['data']['url'],
                            'contents': message['data']['contents'],
                        }

                        if 'username' in message['data']:
                            paste_data['username'] = message['data']['username']

                        if 'date' in message['data']:
                            paste_data['@timestamp'] = datetime.utcfromtimestamp(float(message['data']['date'])).isoformat()
                        else:
                            paste_data['@timestamp'] = datetime.now.isoformat()

                        await queue.put(paste_data)
        except websockets.exceptions.ConnectionClosed:
            pass
        except ConnectionRefusedError:
            asyncio.sleep(5)



# if __name__ == "__main__":
#     logging.basicConfig(format='%(levelname)s:%(filename)s:%(message)s', level=logging.INFO)
#     queue = asyncio.Queue()
#     asyncio.get_event_loop().run_until_complete(
#         produce(queue)
#     )
