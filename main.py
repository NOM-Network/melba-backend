from websockets.client import connect
import websockets.server
from websockets.exceptions import ConnectionClosed
import json
import time
import wave
import io
import aiohttp
from aiohttp import web
import logging
import websockets
from asyncio.queues import Queue, PriorityQueue
from pydub import AudioSegment
import pydub
import asyncio
from dataclasses import dataclass, field
import traceback
import random
import signal
import sys

#logging.basicConfig(level=logging.DEBUG)

from dotenv import load_dotenv
load_dotenv()

import config
import twitch

with open("emotes.txt", "r") as f:
    emotes = f.readlines()
messages_read = {}

# Chat ELO is real
def message_rank(message: str, user_name: str):
    rank = random.randrange(800, 1000)
    if user_name in messages_read:
        rank += 200
    emote_count = 0
    for emote in emotes:
        emote_count += message.count(emote)
    rank += emote_count * 100
    return rank

@dataclass
class SpeechEvent:
    response_text: str = field(default = None, compare = False)
    audio_segment: AudioSegment = field(default = None, compare = False)
    pass

@dataclass(init = False, order = True)
class ChatSpeechEvent(SpeechEvent):
    priority: int = 0
    user_message: str = field(default = None, compare = False)
    user_name: str = field(default = None, compare = False)
    def __init__(self, user_message, user_name):
        self.response_text = None
        self.audio_segment = None
        self.priority = message_rank(user_message, user_name)
        self.user_message = user_message
        self.user_name = user_name
    pass

# Connection to the LLM
class LLM:
    def __init__(self):
        self._websocket_client = None

    async def listen(self):
        async with websockets.server.serve(self._websocket_handler, host = "127.0.0.1", port = 9877) as server:
            await server.serve_forever()

    async def _websocket_handler(self, websocket):
        if self._websocket_client is None:
            print("LLM connected")
            self._websocket_client = websocket
            await websocket.wait_closed()
            print("LLM Websocket handler exited")
            self._websocket_client = None
        else:
            print("LLM already connected, connection rejected")

    async def _recv_message(self):
        if self._websocket_client is None:
            raise Exception("LLM not connected")
        try:
            return await self._websocket_client.recv()
        except ConnectionClosed:
            print("LLM connection closed")
            self._websocket_client = None

    async def _send_message(self, message):
        if self._websocket_client is None:
            raise Exception("LLM not connected")
        try:
            await self._websocket_client.send(message)
        except ConnectionClosed:
            print("LLM connection closed")
            self._websocket_client = None

    async def generate_response(self, prompt, person):
        start = time.time()
        if self._websocket_client is None:
            raise Exception("LLM not connected")
        request = {"message": prompt, "prompt_setting": "generic", "person": person}
        await self._send_message(json.dumps(request))
        llm_response_text = await self._recv_message()
        end = time.time()
        print(f"LLM time: {end - start}s")
        return json.loads(llm_response_text)["response_text"]


chat_messages = PriorityQueue(maxsize=10)
tts_queue = Queue(maxsize=5)
speech_queue = Queue(maxsize=5)

async def llm_loop(llm):
    while True:
        message = await chat_messages.get()
        try:
            response = await llm.generate_response(message.user_message, message.user_name)
        except asyncio.CancelledError as e:
            raise e
        except:
            print("Exception during LLM fetch:")
            print(traceback.format_exc())
            response = None
        if response is not None:
            try:
                message.response_text = response
                await tts_queue.put(message)
            except asyncio.QueueFull:
                print("TTS queue full, dropping message: " + message.response_text)
        else:
            print("LLM failed for message:", message)

async def fetch_tts(text):
    start = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.post(config.tts_url, data = {"text": text, "voice": "voice2", "speed": 1.2, "pitch": 10}) as response:
            end = time.time()
            print(f"TTS time: {end - start}s")
            try:
                response_bytes = await response.read()
                return await asyncio.to_thread(AudioSegment.from_file, io.BytesIO(response_bytes))
            except pydub.exceptions.CouldntDecodeError:
                with open("failed_tts_output", "wb") as binary_file:
                    binary_file.write(await response.read())
                return None

async def tts_loop():
    while True:
        message = await tts_queue.get()
        try:
            response = await fetch_tts(message.response_text)
        except asyncio.CancelledError as e:
            raise e
        except:
            print("Exception during TTS fetch:")
            print(traceback.format_exc())
            response = None
        if response is not None:
            message.audio_segment = response
            await speech_queue.put(message)
        else:
            print("TTS failed for message:", message)

# Connection to Melba Toaster
class Toaster:
    def __init__(self):
        self._websocket_client = None

        self.ready_for_speech = False
        self.tts_paused = False

    async def listen(self):
        async with websockets.server.serve(self._websocket_handler, host = "127.0.0.1", port = 9876) as server:
            await server.serve_forever()

    async def _websocket_handler(self, websocket):
        print("Websocket handler exited")
        if self._websocket_client is None:
            print("Toaster connected")
            self._websocket_client = websocket
            while self._websocket_client is not None:
                try:
                    await self._on_message(await self._websocket_client.recv())
                except ConnectionClosed:
                    print("Toaster connection closed")
                    self._websocket_client = None
                except:
                    print("Exception in Toaster message handler:")
                    print(traceback.format_exc())
            print("Toaster Websocket handler exited")
            self._websocket_client = None
        else:
            print("Toaster already connected, connection rejected")

    async def _on_message(self, message):
        # TODO: parse message
        asyncio.create_task(self._process_ready())

    async def _process_ready(self):
        print("Toaster is now ready for speech")
        self.ready_for_speech = True
        speech_event = await speech_queue.get()
        if self.tts_paused:
            print("TTS paused, speech cancelled")
            return
        self.ready_for_speech = False
        print("Speaking: " + speech_event.response_text)
        print(f"Responding to {speech_event.user_name}: {speech_event.user_message}")
        await self.speak_audio(speech_event.audio_segment, speech_event.user_message, speech_event.response_text)
        messages_read[speech_event.user_name] = messages_read.get(speech_event.user_name, 0) + 1

    async def _send_message(self, message):
        try:
            await self._websocket_client.send(message)
        except ConnectionClosed:
            print("Toaster connection closed")
            self._websocket_client = None

    async def speak_audio(self, audio_segment, prompt, text):
        mp3_file = io.BytesIO()
        await asyncio.to_thread(audio_segment.export, mp3_file, format="mp3")
        await self._send_message(mp3_file.getvalue())
        new_speech = {
                "type": "NewSpeech",
                "prompt": prompt,
                "text": text,
        }
        await self._send_message(json.dumps(new_speech))

async def add_message(message: str, user: str):
    speech_event = ChatSpeechEvent(message, user)
    try:
        chat_messages.put_nowait(speech_event)
        print(f"Chat message added to queue ({user}|{speech_event.priority}): {message}")
    except asyncio.QueueFull:
        # Queue full, message ignored
        pass

async def handle_root(request):
    return web.Response(text="Erm")

async def handle_pause_tts(request):
    toaster = request.app["toaster"]
    toaster.tts_paused = not toaster.tts_paused
    if not toaster.tts_paused:
        # FIXME: only create a new task if it doesn't already exist
        # TODO: fix abstraction break
        asyncio.create_task(toaster._process_ready())
    text = f"TTS paused: {toaster.tts_paused}"
    print(text)
    return web.Response(text=text)

async def handle_speak(request):
    #print(request)
    data = await request.read()
    segment = await asyncio.to_thread(AudioSegment.from_file, io.BytesIO(data))
    toaster = request.app["toaster"]
    prompt = request.headers["prompt"]
    text = request.headers["text"]
    await toaster.speak_audio(segment, prompt, text)
    return web.Response(text=f"Speaking")

async def control_server(toaster):
    app = web.Application()
    app.add_routes([web.get("/", handle_root),
                    web.post("/control/speak", handle_speak),
                    web.get("/control/pause_tts", handle_pause_tts)])
    app["toaster"] = toaster
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9878)
    await site.start()
    print("Control server started")
    while True:
        await asyncio.sleep(1.0)
    await runner.cleanup()
    print("Control server stopped")


async def main():
    toaster = Toaster()
    llm = LLM()
    twitch_chat = twitch.Chat(config.channel, onmessage = add_message)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(toaster.listen())
        tg.create_task(llm.listen())
        tg.create_task(llm_loop(llm))
        tg.create_task(tts_loop())
        tg.create_task(twitch_chat.connect())
        tg.create_task(control_server(toaster))
        print(f"started at {time.strftime('%X')}")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, sys.exit) # TODO: clean shutdown
    loop.create_task(main())
    try:
        loop.run_forever()
    finally:
        loop.close()
