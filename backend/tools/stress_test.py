#!/usr/bin/env python
"""
Stress Test Tool for JARVIS.

Simulates multiple clients connecting and sending audio to test server capacity.
"""

import asyncio
import websockets
import json
import base64
import time
import numpy as np
import argparse
from datetime import datetime

import wave

# Load audio from file
def load_audio_file(filename="tools/test_audio.wav"):
    try:
        with wave.open(filename, 'rb') as wf:
            return wf.readframes(wf.getnframes())
    except FileNotFoundError:
        print(f"Warning: {filename} not found. Using synthetic sine wave.")
        return None

# Generate synthetic audio (sine wave)
def generate_audio_chunk(duration_ms=30, sample_rate=16000, frequency=440):
    t = np.linspace(0, duration_ms/1000, int(sample_rate * duration_ms/1000), False)
    # Generate int16 audio
    audio = (np.sin(frequency * 2 * np.pi * t) * 32767).astype(np.int16)
    return audio.tobytes()

async def run_client(client_id, url, audio_data=None, duration_s=5):
    """Simulate a single client."""
    try:
        print(f"Client {client_id}: Connecting to {url}")
        async with websockets.connect(url) as ws:
            print(f"Client {client_id}: Connected")
            
            # Receive CONNECT message
            await ws.recv()
            
            start_time = time.time()
            
            # Calculate total bytes to send based on duration
            # 16000 Hz * 2 bytes/sample * duration_s
            total_bytes = 16000 * 2 * duration_s
            
            if audio_data:
                # Use real audio
                # Loop the audio if it's shorter than duration
                source_audio = audio_data
                while len(source_audio) < total_bytes:
                    source_audio += audio_data
                audio_to_send = source_audio[:total_bytes]
            else:
                # Use synthetic audio
                audio_to_send = b""
                
            # Send in 30ms chunks (960 bytes)
            chunk_size = 960
            offset = 0
            chunks_sent = 0
            
            while offset < len(audio_to_send) or (audio_data is None and time.time() - start_time < duration_s):
                if audio_data:
                    chunk = audio_to_send[offset:offset+chunk_size]
                    offset += chunk_size
                else:
                    chunk = generate_audio_chunk()
                
                encoded = base64.b64encode(chunk).decode('utf-8')
                
                await ws.send(json.dumps({
                    "id": f"msg-{client_id}-{chunks_sent}",
                    "type": "user_audio",
                    "data": {
                        "audio": encoded,
                        "encoding": "base64"
                    }
                }))
                chunks_sent += 1
                await asyncio.sleep(0.03) # 30ms sleep

                
            print(f"Client {client_id}: Sent {chunks_sent} chunks. Sending silence...")
            
            # Send 2 seconds of silence to trigger VAD end
            silence_chunk = bytes(960) # 30ms of silence (16kHz * 2 bytes * 0.03s = 960 bytes)
            encoded_silence = base64.b64encode(silence_chunk).decode('utf-8')
            
            for _ in range(70): # ~2.1 seconds (70 * 30ms)
                await ws.send(json.dumps({
                    "id": f"msg-{client_id}-silence",
                    "type": "user_audio",
                    "data": {
                        "audio": encoded_silence,
                        "encoding": "base64"
                    }
                }))
                await asyncio.sleep(0.03)

            print(f"Client {client_id}: Waiting for transcript...")
            
            # Wait for response (timeout 15s)
            try:
                async with asyncio.timeout(15):
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        # Log message type
                        msg_type = data.get("type")
                        print(f"Client {client_id} received: {msg_type}")
                        
                        if msg_type == "conversation.transcript":
                            print(f"Client {client_id}: Got Transcript: {data['data']['text']}")
                            break
                        elif msg_type == "system.error":
                             print(f"Client {client_id}: ERROR: {data.get('error')}")
            except asyncio.TimeoutError:
                print(f"Client {client_id}: Timed out waiting for transcript")
                
    except Exception as e:
        print(f"Client {client_id}: Error: {e}")

async def main():
    parser = argparse.ArgumentParser(description="Stress Test JARVIS Backend")
    parser.add_argument("--clients", type=int, default=5, help="Number of concurrent clients")
    parser.add_argument("--duration", type=int, default=3, help="Duration of speech in seconds")
    parser.add_argument("--url", default="ws://localhost:8000/api/v1/ws", help="WebSocket Base URL")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM processing (use 'test-' prefix)")
    args = parser.parse_args()
    
    print(f"Starting Stress Test: {args.clients} clients, {args.duration}s audio")
    print(f"LLM Processing: {'SKIPPED' if args.skip_llm else 'ENABLED'}")
    
    # Load real audio if available
    audio_data = load_audio_file()
    if audio_data:
        print("Using real audio sample: backend/tools/test_audio.wav")
    else:
        print("Using synthetic sine wave (transcription may fail)")
    
    tasks = []
    for i in range(args.clients):
        # Generate session ID
        # If skip-llm is True, use 'test-' prefix.
        # Otherwise use 'stress-' prefix.
        prefix = "test-stress" if args.skip_llm else "stress"
        session_id = f"{prefix}-{i}"
        
        client_url = f"{args.url}/{session_id}"
        tasks.append(run_client(i, client_url, audio_data, args.duration))
        
    start_global = time.time()
    await asyncio.gather(*tasks)
    print(f"Total Test Time: {time.time() - start_global:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())

