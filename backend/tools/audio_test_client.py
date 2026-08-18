#!/usr/bin/env python
"""
Audio Test Client for Jarvis AI Assistant.

Purpose: Test the microphone input, VAD (Silence Detection), and STT (Transcription).
Does NOT display LLM responses or play TTS. 
Use `jarvis_cli.py` for full interaction.
"""

import asyncio
import websockets
import json
import pyaudio
import base64
import argparse
import sys
from datetime import datetime

def list_audio_devices():
    """List all available audio input devices."""
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    num_devices = info.get('deviceCount')
    
    print("\nAvailable Audio Input Devices:")
    print("------------------------------")
    
    input_devices = []
    for i in range(num_devices):
        device_info = p.get_device_info_by_index(i)
        if device_info.get('maxInputChannels') > 0:  # Input device
            input_devices.append((i, device_info))
            print(f"Device {i}: {device_info.get('name')}")
            print(f"  Channels: {device_info.get('maxInputChannels')}")
            print(f"  Default Sample Rate: {device_info.get('defaultSampleRate')}")
            print()
    
    p.terminate()
    return input_devices

async def stream_microphone(server_url: str, session_id: str = None, device_index: int = None):
    """Stream microphone audio to WebSocket server."""
    # Initialize PyAudio
    p = pyaudio.PyAudio()
    
    # If no device specified, use default
    if device_index is None:
        try:
            default_info = p.get_default_input_device_info()
            device_index = default_info['index']
            print(f"Using default input device (index {device_index}): {default_info.get('name')}")
        except IOError:
            print("Error: No default input device found.")
            p.terminate()
            return
    else:
        try:
            device_info = p.get_device_info_by_index(device_index)
            print(f"Using selected input device: {device_info.get('name')} (index {device_index})")
        except IOError:
            print(f"Error: Device index {device_index} not found.")
            p.terminate()
            return
    
    # Open audio stream
    chunk_size = 480 
    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16000,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=chunk_size
    )
    
    # Connect to WebSocket
    # Use 'test-' prefix to tell backend to skip LLM processing
    session = session_id or 'test-audio-client'
    url = f"{server_url}/ws/{session}"
    print(f"Connecting to {url}...")
    
    try:
        async with websockets.connect(url) as websocket:
            print(f"Connected to {url}")
            print("Streaming audio... Press Ctrl+C to stop")
            print("Speak into your microphone to test voice detection")
            
            while True:
                try:
                    # Capture audio
                    audio_data = stream.read(chunk_size, exception_on_overflow=False)
                    
                    # Encode and send
                    encoded = base64.b64encode(audio_data).decode('utf-8')
                    message = {
                        "id": f"msg-{datetime.now().timestamp()}",
                        "type": "user_audio",
                        "data": {
                            "audio": encoded,
                            "encoding": "base64",
                            "sample_rate": 16000,
                            "channels": 1
                        }
                    }
                    await websocket.send(json.dumps(message))
                    
                    # Receive response (non-blocking attempt)
                    try:
                        response = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                        response_data = json.loads(response)
                        
                        # Print speech detection results
                        if response_data.get("type") in ["speech.start", "speech.end"]:
                            is_speech = response_data.get("data", {}).get("is_speech", False)
                            status = "SPEAKING" if is_speech else "SILENT"
                            print(f"\r[{datetime.now().strftime('%H:%M:%S')}] {status}   ", end="", flush=True)
                        
                        # Print transcription results
                        elif response_data.get("type") == "conversation.transcript":
                            transcript = response_data.get("data", {}).get("text", "")
                            print(f"\n[TRANSCRIPT] {transcript}")
                        
                        # IGNORE LLM responses in this tool
                            
                    except asyncio.TimeoutError:
                        pass # No message received, continue streaming
                    
                except websockets.exceptions.ConnectionClosed:
                    print("\nServer closed connection.")
                    break
                except Exception as e:
                    print(f"\nError in loop: {e}")
                    break
                    
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping audio stream...")
    except Exception as e:
        print(f"\nConnection Error: {e}")
    finally:
        if stream.is_active():
            stream.stop_stream()
        stream.close()
        p.terminate()
        print("Audio stream closed. Goodbye.")

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Audio streaming test client")
    parser.add_argument("--server", default="ws://localhost:8000/api/v1", help="WebSocket server URL")
    parser.add_argument("--session", default=None, help="Session ID")
    parser.add_argument("--list-devices", action="store_true", help="List available audio input devices")
    parser.add_argument("--device", type=int, default=None, help="Audio input device index")
    args = parser.parse_args()
    
    if args.list_devices:
        list_audio_devices()
        return
    
    try:
        asyncio.run(stream_microphone(args.server, args.session, args.device))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
