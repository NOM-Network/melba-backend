from websockets.client import connect
import websockets.server
from websockets.exceptions import ConnectionClosed
import json
import time
import wave
import io
import aiohttp
import logging
import websockets
from asyncio.queues import Queue, PriorityQueue
from pydub import AudioSegment
import pydub
import asyncio
from dataclasses import dataclass, field
import traceback
import random

import config
import twitch

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
                return AudioSegment.from_file(io.BytesIO(await response.read()))
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
        self._websocket_clients = []

        self.toast = True
        self.void = False

    async def listen(self):
        async with websockets.server.serve(self._websocket_handler, host = "127.0.0.1", port = 9876) as server:
            await server.serve_forever()

    async def _websocket_handler(self, websocket):
        print("Toaster connected")
        self._websocket_clients.append(websocket)
        while True:
            await asyncio.sleep(.5) # surely nothing will go wrong if i set it to 0.5 :clueless:
        print("Websocket handler exited")

    async def _send_message(self, message):
        for client in self._websocket_clients:
            try:
                await client.send(message)
            except ConnectionClosed:
                print("Toaster connection closed")
                self._websocket_clients.remove(client)

    async def speak_audio(self, audio_segment, prompt, text):
        mp3_file = io.BytesIO()
        audio_segment.export(mp3_file, format="mp3")
        await self._send_message(mp3_file.getvalue())
        new_speech = {
                "type": "NewSpeech",
                "prompt": prompt,
                "text": text,
        }
        await self._send_message(json.dumps(new_speech))
        print("Duration:", audio_segment.duration_seconds)
        await asyncio.sleep(audio_segment.duration_seconds)

async def speech_loop(toaster):
    while True:
        try:
            speech_event = await speech_queue.get()
            print("Speaking: " + speech_event.response_text)
            print("Responding to: " + speech_event.user_message)
            await toaster.speak_audio(speech_event.audio_segment, speech_event.user_message, speech_event.response_text)
            print("Done speaking")
            await asyncio.sleep(3.0)
        except asyncio.CancelledError as e:
            raise e
        except:
            print("Exception during speech:")
            print(traceback.format_exc())

async def add_message(message: str, user: str):
    await chat_messages.put(ChatSpeechEvent(message, user))
    pass

async def main():
    toaster = Toaster()
    llm = LLM()
    twitch_chat = twitch.Chat(config.channel, onmessage = add_message)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(toaster.listen())
        tg.create_task(llm.listen())
        tg.create_task(llm_loop(llm))
        tg.create_task(tts_loop())
        tg.create_task(speech_loop(toaster))
        tg.create_task(twitch_chat.connect())
        print(f"started at {time.strftime('%X')}")

if __name__ == "__main__":
    asyncio.run(main())
