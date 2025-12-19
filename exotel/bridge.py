import os
import sys
import json
import base64
import signal
import logging
import asyncio
import argparse
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from queue import Queue, Empty

import websockets
import struct
from flask import Flask
from flask_sock import Sock
from dotenv import load_dotenv

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


class CallLogger:
    def __init__(self, call_sid: str, stream_sid: str = None):
        self.call_sid = call_sid or f"unknown_{int(time.time())}"
        self.stream_sid = stream_sid
        self.start_time = datetime.now()
        
        timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
        self.filename = f"{timestamp}_{self.call_sid}.txt"
        self.filepath = os.path.join(LOGS_DIR, self.filename)
        
        self.logger = logging.getLogger(f"Call_{self.call_sid}")
        self.logger.setLevel(logging.DEBUG)
        
        # Ensure logs go to stdout for GCP Cloud Logging
        if not self.logger.handlers:
            stream_handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            stream_handler.setFormatter(formatter)
            self.logger.addHandler(stream_handler)
        
        try:
            self.file_handler = logging.FileHandler(self.filepath, mode='w', encoding='utf-8')
            self.file_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            self.file_handler.setFormatter(formatter)
            self.logger.addHandler(self.file_handler)
        except Exception:
            self.file_handler = None
        
        self._write_header()
    
    def _write_header(self):
        self.logger.info("=" * 70)
        self.logger.info(f"CALL LOG - {self.call_sid}")
        self.logger.info("=" * 70)
        self.logger.info(f"Start Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Stream SID: {self.stream_sid}")
        self.logger.info(f"Log File: {self.filename}")
        self.logger.info("=" * 70)
    
    def log(self, level: str, source: str, message: str):
        log_line = f"[{source}] {message}"
        if level == "DEBUG":
            self.logger.debug(log_line)
        elif level == "INFO":
            self.logger.info(log_line)
        elif level == "WARNING":
            self.logger.warning(log_line)
        elif level == "ERROR":
            self.logger.error(log_line)
        else:
            self.logger.info(log_line)
    
    def info(self, source: str, message: str):
        self.log("INFO", source, message)
    
    def debug(self, source: str, message: str):
        self.log("DEBUG", source, message)
    
    def warning(self, source: str, message: str):
        self.log("WARNING", source, message)
    
    def error(self, source: str, message: str):
        self.log("ERROR", source, message)
    
    def log_exotel(self, message: str, level: str = "INFO"):
        self.log(level, "EXOTEL", message)
    
    def log_elevenlabs(self, message: str, level: str = "INFO"):
        self.log(level, "ELEVENLABS", message)
    
    def log_bridge(self, message: str, level: str = "INFO"):
        self.log(level, "BRIDGE", message)
    
    def log_transcript(self, speaker: str, text: str):
        self.logger.info(f"[TRANSCRIPT] {speaker}: {text}")
    
    def close(self):
        end_time = datetime.now()
        duration = (end_time - self.start_time).total_seconds()
        
        self.logger.info("=" * 70)
        self.logger.info("CALL ENDED")
        self.logger.info(f"End Time: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Duration: {duration:.2f} seconds")
        self.logger.info("=" * 70)
        
        self.file_handler.close()
        self.logger.removeHandler(self.file_handler)


def resample_audio(audio_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate:
        return audio_bytes
    
    num_samples = len(audio_bytes) // 2
    samples = struct.unpack(f'<{num_samples}h', audio_bytes)
    
    ratio = to_rate / from_rate
    new_num_samples = int(num_samples * ratio)
    
    if ratio > 1:
        new_samples = []
        for i in range(new_num_samples):
            src_idx = i / ratio
            idx = int(src_idx)
            frac = src_idx - idx
            
            if idx + 1 < num_samples:
                sample = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
            else:
                sample = samples[idx] if idx < num_samples else 0
            new_samples.append(max(-32768, min(32767, sample)))
    else:
        new_samples = []
        for i in range(new_num_samples):
            src_idx = int(i / ratio)
            if src_idx < num_samples:
                new_samples.append(samples[src_idx])
            else:
                new_samples.append(0)
    
    return struct.pack(f'<{len(new_samples)}h', *new_samples)


load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger('Bridge')
exotel_logger = logging.getLogger('Exotel')
elevenlabs_logger = logging.getLogger('ElevenLabs')

app = Flask(__name__)
sock = Sock(app)

EXOTEL_SAMPLE_RATE = 8000
EXOTEL_BIT_DEPTH = 16
EXOTEL_CHANNELS = 1
BYTES_PER_SAMPLE = EXOTEL_BIT_DEPTH // 8

CHUNK_ALIGNMENT = 320
MIN_CHUNK_SIZE = 3200
MAX_CHUNK_SIZE = 102400
DEFAULT_CHUNK_SIZE = 6400


@dataclass
class BridgeConfig:
    elevenlabs_agent_id: str
    elevenlabs_api_key: str
    elevenlabs_region: str = "default"
    exotel_port: int = 10002
    audio_sample_rate: int = 8000
    chunk_size: int = DEFAULT_CHUNK_SIZE
    log_level: str = "INFO"


@dataclass
class StreamMetadata:
    stream_sid: str
    call_sid: str = ""
    account_sid: str = ""
    from_number: str = ""
    to_number: str = ""
    encoding: str = "raw"
    sample_rate: int = 8000
    bit_rate: int = 16
    custom_parameters: Dict[str, str] = field(default_factory=dict)


class AudioBuffer:
    
    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.buffer = Queue()
        self.closed = False
        self.chunk_size = chunk_size
        self._accumulated = bytearray()
    
    def put(self, data: bytes):
        if not self.closed:
            self.buffer.put(data)
    
    def put_raw(self, data: bytes):
        if self.closed:
            return
        
        self._accumulated.extend(data)
        
        while len(self._accumulated) >= self.chunk_size:
            chunk = bytes(self._accumulated[:self.chunk_size])
            self.buffer.put(chunk)
            self._accumulated = self._accumulated[self.chunk_size:]
    
    def flush(self):
        if self._accumulated and not self.closed:
            remainder = len(self._accumulated) % CHUNK_ALIGNMENT
            if remainder:
                padding = CHUNK_ALIGNMENT - remainder
                self._accumulated.extend(b'\x00' * padding)
            
            if len(self._accumulated) >= MIN_CHUNK_SIZE:
                self.buffer.put(bytes(self._accumulated))
            self._accumulated = bytearray()
    
    def get(self, timeout: float = 0.1) -> Optional[bytes]:
        try:
            return self.buffer.get(timeout=timeout)
        except Empty:
            return None
    
    def clear(self):
        self._accumulated = bytearray()
        while not self.buffer.empty():
            try:
                self.buffer.get_nowait()
            except Empty:
                break
    
    def close(self):
        self.closed = True
        self.buffer.put(None)


class ElevenLabsClient:
    
    REGION_URLS = {
        "default": "wss://api.elevenlabs.io",
        "us": "wss://api.us.elevenlabs.io",
        "eu": "wss://api.eu.residency.elevenlabs.io",
        "india": "wss://api.in.residency.elevenlabs.io",
    }
    
    def __init__(self, agent_id: str, api_key: str, region: str = "default", call_logger: CallLogger = None):
        self.agent_id = agent_id
        self.api_key = api_key
        self.base_url = self.REGION_URLS.get(region, self.REGION_URLS["default"])
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.conversation_id: Optional[str] = None
        self.connected = False
        self.audio_format_out: Optional[str] = None
        self.audio_format_in: Optional[str] = None
        self.call_logger = call_logger
        
    @property
    def ws_url(self) -> str:
        return f"{self.base_url}/v1/convai/conversation?agent_id={self.agent_id}"
    
    async def connect(self) -> bool:
        try:
            extra_headers = {}
            if self.api_key:
                extra_headers["xi-api-key"] = self.api_key
            
            elevenlabs_logger.info("=" * 50)
            elevenlabs_logger.info("Connecting to ElevenLabs...")
            elevenlabs_logger.info(f"    URL: {self.ws_url}")
            elevenlabs_logger.info(f"    Agent ID: {self.agent_id}")
            elevenlabs_logger.info(f"    Region: {self.base_url}")
            elevenlabs_logger.info(f"    API Key: {'***' + self.api_key[-4:] if self.api_key else 'Not provided'}")
            elevenlabs_logger.info("=" * 50)
            
            if self.call_logger:
                self.call_logger.log_elevenlabs(f"Connecting to ElevenLabs...")
                self.call_logger.log_elevenlabs(f"URL: {self.ws_url}")
                self.call_logger.log_elevenlabs(f"Agent ID: {self.agent_id}")
            
            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers=extra_headers,
                ping_interval=20,
                ping_timeout=10,
            )
            self.connected = True
            elevenlabs_logger.info("Connected to ElevenLabs successfully!")
            if self.call_logger:
                self.call_logger.log_elevenlabs("Connected to ElevenLabs successfully!")
            return True
        except Exception as e:
            elevenlabs_logger.error(f"Failed to connect to ElevenLabs: {e}")
            if self.call_logger:
                self.call_logger.log_elevenlabs(f"Failed to connect: {e}", "ERROR")
            self.connected = False
            return False
    
    async def send_audio(self, audio_base64: str):
        if self.ws and self.connected:
            message = {"user_audio_chunk": audio_base64}
            elevenlabs_logger.debug(f">>> SEND audio chunk ({len(audio_base64)} chars base64)")
            await self.ws.send(json.dumps(message))
    
    async def send_pong(self, event_id: int):
        if self.ws and self.connected:
            message = {"type": "pong", "event_id": event_id}
            elevenlabs_logger.debug(f">>> SEND pong (event_id: {event_id})")
            await self.ws.send(json.dumps(message))
    
    async def send_conversation_config(self, config_override: dict = None):
        if self.ws and self.connected:
            message = {
                "type": "conversation_initiation_client_data"
            }
            if config_override:
                message["conversation_config_override"] = config_override
            elevenlabs_logger.info(f">>> SEND conversation_initiation_client_data")
            elevenlabs_logger.debug(f">>> Config: {json.dumps(message, indent=2)}")
            await self.ws.send(json.dumps(message))
    
    async def close(self):
        elevenlabs_logger.info("Closing ElevenLabs connection...")
        self.connected = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        elevenlabs_logger.info("ElevenLabs connection closed")


class ConversationBridge:
    
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.elevenlabs: Optional[ElevenLabsClient] = None
        self.exotel_ws = None
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.metadata: Optional[StreamMetadata] = None
        self.active = False
        
        self.user_audio_buffer = AudioBuffer()
        self.agent_audio_buffer = AudioBuffer(chunk_size=config.chunk_size)
        
        self.outbound_sequence = 0
        self.outbound_chunk = 0
        
        self.pending_marks: Dict[str, Any] = {}
        self.mark_counter = 0
        
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        
        self.start_time: float = 0
        
        self.call_logger: Optional[CallLogger] = None
        
    def start(self, exotel_ws, metadata: StreamMetadata):
        self.exotel_ws = exotel_ws
        self.stream_sid = metadata.stream_sid
        self.call_sid = metadata.call_sid
        self.metadata = metadata
        self.active = True
        self.start_time = time.time()
        
        self.call_logger = CallLogger(call_sid=metadata.call_sid, stream_sid=metadata.stream_sid)
        self.call_logger.log_exotel(f"Call started")
        self.call_logger.log_exotel(f"From: {metadata.from_number}")
        self.call_logger.log_exotel(f"To: {metadata.to_number}")
        self.call_logger.log_exotel(f"Account SID: {metadata.account_sid}")
        self.call_logger.log_exotel(f"Encoding: {metadata.encoding}, Sample Rate: {metadata.sample_rate}, Bit Rate: {metadata.bit_rate}")
        if metadata.custom_parameters:
            self.call_logger.log_exotel(f"Custom Parameters: {json.dumps(metadata.custom_parameters)}")
        
        self.outbound_sequence = 0
        self.outbound_chunk = 0
        self.mark_counter = 0
        
        self.user_audio_buffer = AudioBuffer()
        self.agent_audio_buffer = AudioBuffer(chunk_size=self.config.chunk_size)
        
        self.elevenlabs_thread = threading.Thread(
            target=self._run_elevenlabs_client,
            daemon=True
        )
        self.elevenlabs_thread.start()
        
        self.playback_thread = threading.Thread(
            target=self._playback_agent_audio,
            daemon=True
        )
        self.playback_thread.start()
        
        logger.info(f"Bridge started for stream: {self.stream_sid}")
        self.call_logger.log_bridge(f"Bridge started for stream: {self.stream_sid}")
    
    def _run_elevenlabs_client(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            self.loop.run_until_complete(self._elevenlabs_session())
        except Exception as e:
            logger.error(f"ElevenLabs session error: {e}")
        finally:
            self.loop.close()
    
    async def _elevenlabs_session(self):
        logger.info("Starting ElevenLabs session...")
        if self.call_logger:
            self.call_logger.log_bridge("Starting ElevenLabs session...")
        
        self.elevenlabs = ElevenLabsClient(
            agent_id=self.config.elevenlabs_agent_id,
            api_key=self.config.elevenlabs_api_key,
            region=self.config.elevenlabs_region,
            call_logger=self.call_logger
        )
        
        if not await self.elevenlabs.connect():
            logger.error("Failed to establish ElevenLabs connection")
            if self.call_logger:
                self.call_logger.log_bridge("Failed to establish ElevenLabs connection", "ERROR")
            return
        
        await self.elevenlabs.send_conversation_config()
        
        logger.info("ElevenLabs session active - starting audio streams")
        if self.call_logger:
            self.call_logger.log_bridge("ElevenLabs session active - starting audio streams")
        
        await asyncio.gather(
            self._send_audio_to_elevenlabs(),
            self._receive_from_elevenlabs(),
            return_exceptions=True
        )
        
        logger.info("ElevenLabs session ended")
        if self.call_logger:
            self.call_logger.log_bridge("ElevenLabs session ended")
    
    async def _send_audio_to_elevenlabs(self):
        elevenlabs_logger.info("Starting audio sender thread (Exotel -> ElevenLabs)")
        chunks_sent = 0
        
        while self.active and self.elevenlabs and self.elevenlabs.connected:
            audio_data = self.user_audio_buffer.get(timeout=0.05)
            
            if audio_data:
                if isinstance(audio_data, bytes):
                    audio_base64 = base64.b64encode(audio_data).decode('ascii')
                else:
                    audio_base64 = audio_data
                
                await self.elevenlabs.send_audio(audio_base64)
                chunks_sent += 1
                
                if chunks_sent == 1:
                    elevenlabs_logger.info(f">>> SEND - First user audio chunk sent to ElevenLabs")
            
            await asyncio.sleep(0.01)
        
        elevenlabs_logger.info(f"Audio sender stopped (sent {chunks_sent} chunks)")
    
    async def _receive_from_elevenlabs(self):
        elevenlabs_logger.info("Starting receiver thread (ElevenLabs -> Bridge)")
        messages_received = 0
        
        while self.active and self.elevenlabs and self.elevenlabs.connected:
            try:
                if self.elevenlabs.ws:
                    message = await asyncio.wait_for(
                        self.elevenlabs.ws.recv(),
                        timeout=0.1
                    )
                    messages_received += 1
                    data = json.loads(message)
                    await self._handle_elevenlabs_message(data)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                elevenlabs_logger.info("ElevenLabs WebSocket connection closed")
                break
            except json.JSONDecodeError as e:
                elevenlabs_logger.error(f"JSON decode error: {e}")
            except Exception as e:
                elevenlabs_logger.error(f"Error receiving from ElevenLabs: {e}")
                await asyncio.sleep(0.1)
        
        elevenlabs_logger.info(f"Receiver stopped (received {messages_received} messages)")
    
    async def _handle_elevenlabs_message(self, data: dict):
        msg_type = data.get("type")
        
        if msg_type != "audio":
            elevenlabs_logger.info(f"<<< RECV [{msg_type}]")
        else:
            elevenlabs_logger.debug(f"<<< RECV [{msg_type}]")
        
        if msg_type == "conversation_initiation_metadata":
            event = data.get("conversation_initiation_metadata_event", {})
            self.elevenlabs.conversation_id = event.get("conversation_id")
            self.elevenlabs.audio_format_out = event.get("agent_output_audio_format")
            self.elevenlabs.audio_format_in = event.get("user_input_audio_format")
            elevenlabs_logger.info(f"    Conversation ID: {self.elevenlabs.conversation_id}")
            elevenlabs_logger.info(f"    Agent Output Audio Format: {self.elevenlabs.audio_format_out}")
            elevenlabs_logger.info(f"    User Input Audio Format: {self.elevenlabs.audio_format_in}")
            elevenlabs_logger.debug(f"    Full event: {json.dumps(event, indent=2)}")
            if self.call_logger:
                self.call_logger.log_elevenlabs(f"Conversation ID: {self.elevenlabs.conversation_id}")
                self.call_logger.log_elevenlabs(f"Audio Format - Out: {self.elevenlabs.audio_format_out}, In: {self.elevenlabs.audio_format_in}")
        
        elif msg_type == "audio":
            audio_event = data.get("audio_event", {})
            audio_base64 = audio_event.get("audio_base_64")
            event_id = audio_event.get("event_id")
            if audio_base64:
                audio_bytes = base64.b64decode(audio_base64)
                elevenlabs_logger.debug(f"    Audio chunk received (event_id: {event_id}, {len(audio_bytes)} bytes)")
                self.agent_audio_buffer.put(audio_bytes)
        
        elif msg_type == "agent_response":
            event = data.get("agent_response_event", {})
            response = event.get("agent_response", "")
            elevenlabs_logger.info(f"    Agent says: \"{response}\"")
            if self.call_logger:
                self.call_logger.log_transcript("AGENT", response)
        
        elif msg_type == "user_transcript":
            event = data.get("user_transcription_event", {})
            transcript = event.get("user_transcript", "")
            elevenlabs_logger.info(f"    User said: \"{transcript}\"")
            if self.call_logger:
                self.call_logger.log_transcript("USER", transcript)
        
        elif msg_type == "ping":
            ping_event = data.get("ping_event", {})
            event_id = ping_event.get("event_id")
            ping_ms = ping_event.get("ping_ms")
            elevenlabs_logger.debug(f"    Ping received (event_id: {event_id}, ping_ms: {ping_ms})")
            if event_id is not None:
                await self.elevenlabs.send_pong(event_id)
        
        elif msg_type == "interruption":
            event = data.get("interruption_event", {})
            event_id = event.get("event_id")
            elevenlabs_logger.info(f"    User interrupted agent (event_id: {event_id})")
            if self.call_logger:
                self.call_logger.log_elevenlabs("User interrupted agent")
            self.agent_audio_buffer.clear()
            self._send_clear_to_exotel()
        
        elif msg_type == "agent_response_correction":
            event = data.get("agent_response_correction_event", {})
            original = event.get("original_agent_response", "")
            corrected = event.get("corrected_agent_response", "")
            elevenlabs_logger.info(f"    Agent correction:")
            elevenlabs_logger.info(f"       Original: \"{original}\"")
            elevenlabs_logger.info(f"       Corrected: \"{corrected}\"")
        
        elif msg_type == "client_tool_call":
            tool_call = data.get("client_tool_call", {})
            tool_name = tool_call.get("tool_name")
            tool_call_id = tool_call.get("tool_call_id")
            parameters = tool_call.get("parameters", {})
            elevenlabs_logger.info(f"    Tool call: {tool_name}")
            elevenlabs_logger.info(f"       Call ID: {tool_call_id}")
            elevenlabs_logger.info(f"       Parameters: {json.dumps(parameters)}")
        
        elif msg_type == "vad_score":
            event = data.get("vad_score_event", {})
            vad_score = event.get("vad_score")
            elevenlabs_logger.debug(f"    VAD score: {vad_score}")
        
        elif msg_type == "internal_tentative_agent_response":
            event = data.get("tentative_agent_response_internal_event", {})
            tentative = event.get("tentative_agent_response", "")
            elevenlabs_logger.debug(f"    Tentative response: \"{tentative}\"")
        
        elif msg_type == "contextual_update":
            text = data.get("text", "")
            elevenlabs_logger.info(f"    Contextual update: \"{text}\"")
        
        else:
            elevenlabs_logger.warning(f"    Unhandled message type: {msg_type}")
            elevenlabs_logger.debug(f"    Full data: {json.dumps(data, indent=2)}")
    
    def _send_audio_to_exotel(self, audio_bytes: bytes):
        if not self.exotel_ws or not self.active:
            return
        
        CHUNK_ALIGNMENT = 320
        TARGET_CHUNK_SIZE = 3200
        BYTES_PER_SECOND = 16000
        
        try:
            offset = 0
            while offset < len(audio_bytes) and self.active:
                chunk = audio_bytes[offset:offset + TARGET_CHUNK_SIZE]
                original_chunk_size = len(chunk)
                
                remainder = len(chunk) % CHUNK_ALIGNMENT
                if remainder != 0:
                    padding = CHUNK_ALIGNMENT - remainder
                    chunk = chunk + (b'\x00' * padding)
                
                if len(chunk) < 320:
                    offset += original_chunk_size
                    continue
                
                audio_base64 = base64.b64encode(chunk).decode('ascii')
                
                elapsed_ms = int((time.time() - self.start_time) * 1000)
                
                self.outbound_sequence += 1
                self.outbound_chunk += 1
                
                message = json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "payload": audio_base64,
                        "timestamp": str(elapsed_ms),
                        "sequenceNumber": str(self.outbound_sequence)
                    }
                })
                self.exotel_ws.send(message)
                
                if self.outbound_chunk <= 3 or self.outbound_chunk % 20 == 0:
                    exotel_logger.info(f">>> SEND [media] chunk={self.outbound_chunk}, {len(chunk)} bytes, ts={elapsed_ms}ms")
                
                offset += TARGET_CHUNK_SIZE
                
                chunk_duration_sec = len(chunk) / BYTES_PER_SECOND
                time.sleep(chunk_duration_sec * 0.8)
            
        except Exception as e:
            exotel_logger.error(f"Error sending audio to Exotel: {e}")
    
    def _send_clear_to_exotel(self):
        if self.exotel_ws and not self.exotel_ws.closed:
            try:
                message = json.dumps({
                    "event": "clear",
                    "stream_sid": self.stream_sid
                })
                self.exotel_ws.send(message)
                exotel_logger.info(f">>> SEND [clear] - Clearing pending audio")
            except Exception as e:
                exotel_logger.error(f"Error sending clear to Exotel: {e}")
    
    def _send_mark_to_exotel(self, mark_name: str):
        if self.exotel_ws and not self.exotel_ws.closed:
            self.outbound_sequence += 1
            try:
                message = json.dumps({
                    "event": "mark",
                    "sequence_number": self.outbound_sequence,
                    "stream_sid": self.stream_sid,
                    "mark": {
                        "name": mark_name
                    }
                })
                self.exotel_ws.send(message)
                exotel_logger.debug(f">>> SEND [mark] - name: {mark_name}")
            except Exception as e:
                exotel_logger.error(f"Error sending mark to Exotel: {e}")
    
    def _playback_agent_audio(self):
        exotel_logger.info("Starting agent audio playback thread")
        
        CHUNK_ALIGNMENT = 320
        TARGET_CHUNK_SIZE = 3200
        BYTES_PER_SECOND = 16000
        
        accumulated_audio = bytearray()
        
        while self.active and self.exotel_ws:
            audio_bytes = self.agent_audio_buffer.get(timeout=0.1)
            
            if audio_bytes:
                accumulated_audio.extend(audio_bytes)
            
            while len(accumulated_audio) >= TARGET_CHUNK_SIZE and self.active:
                chunk = bytes(accumulated_audio[:TARGET_CHUNK_SIZE])
                accumulated_audio = accumulated_audio[TARGET_CHUNK_SIZE:]
                
                try:
                    audio_base64 = base64.b64encode(chunk).decode('ascii')
                    
                    elapsed_ms = int((time.time() - self.start_time) * 1000)
                    
                    self.outbound_sequence += 1
                    self.outbound_chunk += 1
                    
                    message = json.dumps({
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {
                            "payload": audio_base64,
                            "timestamp": str(elapsed_ms),
                            "sequenceNumber": str(self.outbound_sequence)
                        }
                    })
                    self.exotel_ws.send(message)
                    
                    if self.outbound_chunk <= 5 or self.outbound_chunk % 20 == 0:
                        exotel_logger.info(f">>> SEND [media] chunk={self.outbound_chunk}, {len(chunk)} bytes, ts={elapsed_ms}ms")
                    
                    chunk_duration = len(chunk) / BYTES_PER_SECOND
                    time.sleep(chunk_duration * 0.9)
                    
                except Exception as e:
                    exotel_logger.error(f"Error sending audio to Exotel: {e}")
                    break
        
        if accumulated_audio and self.active:
            remainder = len(accumulated_audio) % CHUNK_ALIGNMENT
            if remainder:
                accumulated_audio.extend(b'\x00' * (CHUNK_ALIGNMENT - remainder))
            
            if len(accumulated_audio) >= CHUNK_ALIGNMENT:
                try:
                    audio_base64 = base64.b64encode(bytes(accumulated_audio)).decode('ascii')
                    elapsed_ms = int((time.time() - self.start_time) * 1000)
                    self.outbound_sequence += 1
                    self.outbound_chunk += 1
                    
                    message = json.dumps({
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {
                            "payload": audio_base64,
                            "timestamp": str(elapsed_ms),
                            "sequenceNumber": str(self.outbound_sequence)
                        }
                    })
                    self.exotel_ws.send(message)
                    exotel_logger.info(f">>> SEND [media] final chunk={self.outbound_chunk}, {len(accumulated_audio)} bytes")
                except:
                    pass
        
        exotel_logger.info(f"Agent audio playback stopped (sent {self.outbound_chunk} chunks)")
    
    def process_exotel_audio(self, payload: str, chunk: int = None, timestamp: str = None):
        if self.active:
            audio_bytes = base64.b64decode(payload)
            self.user_audio_buffer.put(audio_bytes)
    
    def process_dtmf(self, digit: str, duration: int = None):
        exotel_logger.info(f"    DTMF digit: {digit} (duration: {duration}ms)")
        if self.call_logger:
            self.call_logger.log_exotel(f"DTMF digit: {digit} (duration: {duration}ms)")
    
    def handle_exotel_mark(self, mark_name: str):
        exotel_logger.info(f"    Mark completed: {mark_name}")
        if mark_name in self.pending_marks:
            del self.pending_marks[mark_name]
    
    def stop(self):
        logger.info(f"Stopping bridge for stream: {self.stream_sid}")
        if self.call_logger:
            self.call_logger.log_bridge(f"Stopping bridge for stream: {self.stream_sid}")
        
        self.active = False
        
        self.agent_audio_buffer.flush()
        
        self.user_audio_buffer.close()
        self.agent_audio_buffer.close()
        
        if self.loop and self.elevenlabs:
            asyncio.run_coroutine_threadsafe(
                self.elevenlabs.close(),
                self.loop
            )
        
        if self.call_logger:
            self.call_logger.close()
            self.call_logger = None


# Global configuration
config: Optional[BridgeConfig] = None

def init_config():
    global config
    # Try to load from environment variables for cloud hosting
    agent_id = os.getenv('ELEVENLABS_AGENT_ID')
    api_key = os.getenv('ELEVENLABS_API_KEY')
    
    if agent_id:
        config = BridgeConfig(
            elevenlabs_agent_id=agent_id,
            elevenlabs_api_key=api_key or "",
            elevenlabs_region=os.getenv('ELEVENLABS_REGION', 'default'),
            exotel_port=int(os.getenv('BRIDGE_PORT', 10002)),
            chunk_size=int(os.getenv('CHUNK_SIZE', DEFAULT_CHUNK_SIZE)),
            log_level=os.getenv('LOG_LEVEL', 'INFO')
        )
        
        log_level = getattr(logging, config.log_level.upper(), logging.INFO)
        logger.setLevel(log_level)
        exotel_logger.setLevel(log_level)
        elevenlabs_logger.setLevel(log_level)
        app.logger.setLevel(log_level)

# Initialize config on module load
init_config()

active_bridges: dict = {}


@sock.route('/media')
def handle_exotel_stream(ws):
    exotel_logger.info("=" * 60)
    exotel_logger.info("New WebSocket connection accepted")
    exotel_logger.info("=" * 60)
    
    bridge: Optional[ConversationBridge] = None
    stream_sid: Optional[str] = None
    message_count = 0
    media_message_count = 0
    
    try:
        while True:
            try:
                message = ws.receive(timeout=1)
            except Exception as recv_error:
                if "closed" in str(recv_error).lower():
                    exotel_logger.info("WebSocket connection closed by remote")
                    break
                continue
            
            if message is None:
                continue
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                exotel_logger.warning(f"<<< RECV [INVALID JSON]: {message[:200]}...")
                continue
            
            event_type = data.get("event")
            sequence_number = data.get("sequence_number")
            message_count += 1
            
            if event_type != "media":
                exotel_logger.info(f"<<< RECV [{event_type}] seq={sequence_number}")
            
            if event_type == "connected":
                exotel_logger.info(f"    Protocol: {data.get('protocol', 'N/A')}")
                exotel_logger.info(f"    Version: {data.get('version', 'N/A')}")
                exotel_logger.debug(f"    Full message: {json.dumps(data, indent=2)}")
            
            elif event_type == "start":
                start_data = data.get("start", {})
                stream_sid = data.get("stream_sid") or start_data.get("stream_sid")
                
                media_format = start_data.get("media_format", {})
                
                sample_rate_raw = media_format.get("sample_rate", 8000)
                try:
                    sample_rate = int(str(sample_rate_raw).replace("Hz", "").replace("hz", "").strip())
                except:
                    sample_rate = 8000
                
                bit_rate_raw = media_format.get("bit_rate", 16)
                try:
                    bit_rate_str = str(bit_rate_raw).lower().replace("kbps", "").replace("bps", "").strip()
                    bit_rate = int(bit_rate_str)
                except:
                    bit_rate = 16
                
                metadata = StreamMetadata(
                    stream_sid=stream_sid,
                    call_sid=start_data.get("call_sid", ""),
                    account_sid=start_data.get("account_sid", ""),
                    from_number=start_data.get("from", ""),
                    to_number=start_data.get("to", ""),
                    encoding=media_format.get("encoding", "raw"),
                    sample_rate=sample_rate,
                    bit_rate=bit_rate,
                    custom_parameters=start_data.get("custom_parameters", {})
                )
                
                exotel_logger.info(f"    Stream SID: {metadata.stream_sid}")
                exotel_logger.info(f"    Call SID: {metadata.call_sid}")
                exotel_logger.info(f"    Account SID: {metadata.account_sid}")
                exotel_logger.info(f"    From: {metadata.from_number}")
                exotel_logger.info(f"    To: {metadata.to_number}")
                exotel_logger.info(f"    Media Format: encoding={metadata.encoding}, sample_rate={metadata.sample_rate}, bit_rate={metadata.bit_rate}")
                if metadata.custom_parameters:
                    exotel_logger.info(f"    Custom Parameters: {json.dumps(metadata.custom_parameters)}")
                exotel_logger.debug(f"    Full start data: {json.dumps(start_data, indent=2)}")
                
                bridge = ConversationBridge(config)
                bridge.start(ws, metadata)
                active_bridges[stream_sid] = bridge
            
            elif event_type == "media":
                media_message_count += 1
                if bridge:
                    media_data = data.get("media", {})
                    payload = media_data.get("payload")
                    chunk = media_data.get("chunk")
                    timestamp = media_data.get("timestamp")
                    
                    if media_message_count == 1:
                        exotel_logger.info(f"<<< RECV [media] - First audio chunk received")
                        exotel_logger.info(f"    Chunk: {chunk}")
                        exotel_logger.info(f"    Timestamp: {timestamp}ms")
                        if payload:
                            decoded_size = len(base64.b64decode(payload))
                            exotel_logger.info(f"    Payload: {len(payload)} base64 chars ({decoded_size} bytes)")
                    elif media_message_count % 100 == 0:
                        exotel_logger.debug(f"<<< RECV [media] - Chunk #{chunk} (ts: {timestamp}ms)")
                    
                    if payload:
                        bridge.process_exotel_audio(payload, chunk, timestamp)
            
            elif event_type == "dtmf":
                dtmf_data = data.get("dtmf", {})
                digit = dtmf_data.get("digit")
                duration = dtmf_data.get("duration")
                exotel_logger.info(f"    Digit: {digit}")
                exotel_logger.info(f"    Duration: {duration}ms")
                if bridge:
                    bridge.process_dtmf(digit, duration)
            
            elif event_type == "mark":
                mark_data = data.get("mark", {})
                mark_name = mark_data.get("name", "N/A")
                exotel_logger.info(f"    Mark name: {mark_name}")
                if bridge:
                    bridge.handle_exotel_mark(mark_name)
            
            elif event_type == "clear":
                exotel_logger.info(f"    Clear event received")
                if bridge:
                    bridge.agent_audio_buffer.clear()
            
            elif event_type == "stop":
                stop_data = data.get("stop", {})
                reason = stop_data.get("reason", "N/A")
                call_sid = stop_data.get("call_sid", "N/A")
                account_sid = stop_data.get("account_sid", "N/A")
                exotel_logger.info(f"    Reason: {reason}")
                exotel_logger.info(f"    Call SID: {call_sid}")
                exotel_logger.info(f"    Account SID: {account_sid}")
                break
            
            else:
                exotel_logger.warning(f"    Unknown event type: {event_type}")
                exotel_logger.debug(f"    Full data: {json.dumps(data, indent=2)}")
    
    except Exception as e:
        exotel_logger.error(f"Error in handler: {e}", exc_info=True)
    
    finally:
        if bridge:
            bridge.stop()
        if stream_sid and stream_sid in active_bridges:
            del active_bridges[stream_sid]
        
        exotel_logger.info("=" * 60)
        exotel_logger.info(f"Connection closed")
        exotel_logger.info(f"    Total messages: {message_count}")
        exotel_logger.info(f"    Media messages: {media_message_count}")
        exotel_logger.info("=" * 60)


@app.route('/health')
def health_check():
    return json.dumps({
        "status": "healthy",
        "active_calls": len(active_bridges),
        "config": {
            "chunk_size": config.chunk_size if config else DEFAULT_CHUNK_SIZE,
            "sample_rate": EXOTEL_SAMPLE_RATE
        }
    }), 200, {'Content-Type': 'application/json'}


def signal_handler(sig, frame):
    logger.info("Shutting down...")
    
    for bridge in active_bridges.values():
        bridge.stop()
    
    sys.exit(0)


def main():
    global config
    
    parser = argparse.ArgumentParser(
        description='Exotel <-> ElevenLabs Conversational AI Bridge'
    )
    parser.add_argument(
        '--port', 
        type=int, 
        default=int(os.getenv('BRIDGE_PORT', 10002)),
        help='Port for the WebSocket server (default: 10002)'
    )
    parser.add_argument(
        '--agent-id',
        type=str,
        default=os.getenv('ELEVENLABS_AGENT_ID'),
        help='ElevenLabs Agent ID'
    )
    parser.add_argument(
        '--api-key',
        type=str,
        default=os.getenv('ELEVENLABS_API_KEY'),
        help='ElevenLabs API Key'
    )
    parser.add_argument(
        '--region',
        type=str,
        default=os.getenv('ELEVENLABS_REGION', 'default'),
        choices=['default', 'us', 'eu', 'india'],
        help='ElevenLabs region (default: default)'
    )
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=int(os.getenv('CHUNK_SIZE', DEFAULT_CHUNK_SIZE)),
        help=f'Audio chunk size in bytes (default: {DEFAULT_CHUNK_SIZE}, must be multiple of {CHUNK_ALIGNMENT})'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default=os.getenv('LOG_LEVEL', 'INFO'),
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level'
    )
    
    args = parser.parse_args()
    
    if args.chunk_size % CHUNK_ALIGNMENT != 0:
        parser.error(f"--chunk-size must be a multiple of {CHUNK_ALIGNMENT} bytes")
    if args.chunk_size < MIN_CHUNK_SIZE:
        parser.error(f"--chunk-size must be at least {MIN_CHUNK_SIZE} bytes")
    if args.chunk_size > MAX_CHUNK_SIZE:
        parser.error(f"--chunk-size must be at most {MAX_CHUNK_SIZE} bytes")
    
    if not args.agent_id:
        parser.error("--agent-id is required (or set ELEVENLABS_AGENT_ID env var)")
    
    if not args.api_key:
        logger.warning("No API key provided. Some features may not work.")
    
    log_level = getattr(logging, args.log_level.upper())
    logger.setLevel(log_level)
    exotel_logger.setLevel(log_level)
    elevenlabs_logger.setLevel(log_level)
    app.logger.setLevel(log_level)
    
    config = BridgeConfig(
        elevenlabs_agent_id=args.agent_id,
        elevenlabs_api_key=args.api_key or "",
        elevenlabs_region=args.region,
        exotel_port=args.port,
        chunk_size=args.chunk_size,
        log_level=args.log_level
    )
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("\n" + "=" * 60)
    print("  Exotel <-> ElevenLabs Conversational AI Bridge")
    print("=" * 60)
    print(f"  Server:      http://0.0.0.0:{args.port}")
    print(f"  WebSocket:   ws://0.0.0.0:{args.port}/media")
    print(f"  Health:      http://0.0.0.0:{args.port}/health")
    print("-" * 60)
    print(f"  Agent ID:    {args.agent_id}")
    print(f"  Region:      {args.region}")
    print(f"  Chunk Size:  {args.chunk_size} bytes ({args.chunk_size // 16}ms @ 8kHz)")
    print(f"  Log Level:   {args.log_level}")
    print("=" * 60 + "\n")
    
    app.run(host='0.0.0.0', port=args.port, debug=False)


if __name__ == '__main__':
    main()