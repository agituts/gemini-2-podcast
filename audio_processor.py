# audio_processor.py

import asyncio
import base64
import json
import os
import wave
from websockets.asyncio.client import connect
import websockets
import pyaudio
from dotenv import load_dotenv
import sys

load_dotenv()

GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')

if sys.version_info < (3, 11):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

class AudioGenerator:
    def __init__(self, voice):
        self.voice = voice
        self.audio_in_queue = asyncio.Queue()
        self.ws = None
        self.ws_semaphore = asyncio.Semaphore(1)

        # Audio configuration
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 2
        self.SAMPLE_RATE = 24000
        self.CHUNK_SIZE = 512

        # WebSocket configuration
        self.ws_options = {
            'ping_interval': 20,
            'ping_timeout': 10,
            'close_timeout': 5
        }

        # API configuration
        self.host = 'generativelanguage.googleapis.com'
        self.model = "gemini-2.0-flash-exp"
        self.uri = f"wss://{self.host}/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={GOOGLE_API_KEY}"

        self.complete_audio = bytearray()

    async def process_batch(self, dialogues, output_files):
        ws = await connect(self.uri, **self.ws_options)
        async with ws:
            self.ws = ws
            await self.startup(ws, self.voice)
            for dialogue, output_file in zip(dialogues, output_files):
                await self.send_text(ws, dialogue)
                await self.receive_audio(output_file)

    async def startup(self, ws, voice):
        async with self.ws_semaphore:
            setup_msg = {
                "setup": {
                    "model": f"models/{self.model}",
                    "generation_config": {
                        "speech_config": {
                            "voice_config": {
                                "prebuilt_voice_config": {
                                    "voice_name": voice
                                }
                            }
                        }
                    }
                }
            }
            await ws.send(json.dumps(setup_msg))
            response = await ws.recv()  # You might want to handle this response

    async def send_text(self, ws, text):
        async with self.ws_semaphore:
            msg = {
                "client_content": {
                    "turn_complete": True,
                    "turns": [
                        {"role": "user", "parts": [{"text": text}]}
                    ]
                }
            }
            await ws.send(json.dumps(msg))

    async def receive_audio(self, output_file):
        async with self.ws_semaphore:
            self.complete_audio.clear()
            await asyncio.sleep(0.1)

            try:
                async for raw_response in self.ws:
                    response = json.loads(raw_response)

                    try:
                        parts = response["serverContent"]["modelTurn"]["parts"]
                        for part in parts:
                            if "inlineData" in part:
                                b64data = part["inlineData"]["data"]
                                pcm_data = base64.b64decode(b64data)
                                self.complete_audio.extend(pcm_data)
                                self.audio_in_queue.put_nowait(pcm_data)
                    except KeyError:
                        pass

                    try:
                        if response["serverContent"].get("turnComplete", False):
                            self.save_wav_file(output_file)
                            while not self.audio_in_queue.empty():
                                self.audio_in_queue.get_nowait()
                            break
                    except KeyError:
                        pass

            except websockets.exceptions.ConnectionClosedError as e:
                print(f"Connection closed: {e}")
                raise

    def save_wav_file(self, filename):
        with wave.open(filename, 'wb') as wav_file:
            wav_file.setnchannels(self.CHANNELS)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.SAMPLE_RATE)
            stereo_data = bytearray()
            for i in range(0, len(self.complete_audio), 2):
                sample = self.complete_audio[i:i+2]
                # Convert mono to stereo by duplicating the sample
                stereo_data.extend(sample)
                stereo_data.extend(sample)
            wav_file.writeframes(stereo_data)

    async def run(self, dialogues, output_files, max_retries=3):
        last_exception = None
        for attempt in range(max_retries):
            try:
                ws = await connect(self.uri, **self.ws_options)
                async with ws:
                    self.ws = ws
                    await self.startup(self.ws, self.voice)
                    for dialogue, output_file in zip(dialogues, output_files):
                        await self.send_text(self.ws, dialogue)
                        await self.receive_audio(output_file)
                return
            except websockets.exceptions.ConnectionClosedError as e:
                last_exception = e
                if attempt < max_retries - 1:
                    print(f"Connection lost. Retrying in 5 seconds... (Attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(5)
                else:
                    print("Max retries reached. Unable to reconnect.")
                    raise last_exception