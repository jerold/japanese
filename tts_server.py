#!/usr/bin/env python3
"""
tts_server.py — Long-running TTS server that keeps Kokoro pipelines loaded.

Listens on a Unix domain socket and synthesizes text on demand, eliminating
the ~4.4s model-loading cost per invocation. Uses a JSON-line protocol:

    → {"text": "Hello 犬", "en_voice": "af_heart", "jp_voice": "jf_alpha", "switch_gap_ms": 60}
    ← {"status": "ok", "sample_rate": 24000, "num_samples": N, "byte_length": M}
    ← <M bytes of raw float32 PCM>

    Cloned voice (English-only) via Chatterbox, loaded lazily on first use:
    → {"text": "Hello", "engine": "chatterbox", "voice": "voices/jerold.wav"}

    → {"command": "ping"}      ← {"status": "ok", "message": "pong"}
    → {"command": "shutdown"}  ← {"status": "ok", "message": "shutting down"}

USAGE
-----
    python tts_server.py
    python tts_server.py --socket /tmp/kokoro-tts.sock
"""

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

from readme_to_mp3 import SAMPLE_RATE, segment_by_language, JAPANESE_PATTERN

DEFAULT_SOCKET = '/tmp/kokoro-tts.sock'
DEFAULT_PID = '/tmp/kokoro-tts.pid'
DEFAULT_IDLE_TIMEOUT = 600  # seconds; 0 disables

PROJECT_DIR = Path(__file__).resolve().parent
VOICES_DIR = PROJECT_DIR / 'voices'
REGISTRY_PATH = VOICES_DIR / 'registry.json'

last_activity = time.monotonic()

_chatterbox = None
_chatterbox_lock = threading.Lock()


def load_pipelines():
    from kokoro import KModel, KPipeline
    print('Loading Kokoro model...', flush=True)
    model = KModel(repo_id='hexgrad/Kokoro-82M')
    print('Creating EN pipeline...', flush=True)
    en = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', model=model)
    print('Creating JP pipeline...', flush=True)
    jp = KPipeline(lang_code='j', repo_id='hexgrad/Kokoro-82M', model=model)
    return en, jp


def get_chatterbox():
    """Lazy-load the Chatterbox cloning model on first use."""
    global _chatterbox
    if _chatterbox is not None:
        return _chatterbox
    with _chatterbox_lock:
        if _chatterbox is None:
            print('Loading Chatterbox model (first use; ~2GB)...', flush=True)
            from chatterbox.tts import ChatterboxTTS
            _chatterbox = ChatterboxTTS.from_pretrained(device='mps')
            print('Chatterbox ready.', flush=True)
    return _chatterbox


def _load_registry() -> dict:
    """Load voices/registry.json if present, return {} otherwise."""
    try:
        with open(REGISTRY_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_voice_ref(voice: str) -> Path:
    """Resolve a voice argument to an absolute WAV path.

    Accepts either a nickname registered in voices/registry.json or a
    filesystem path (absolute, or relative to the project directory).
    Raises ValueError if the resolved file does not exist.
    """
    if not voice:
        raise ValueError('chatterbox engine requires a voice (path or nickname)')

    registry = _load_registry()
    if voice in registry:
        candidate = Path(registry[voice])
    else:
        candidate = Path(voice)

    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate

    if not candidate.is_file():
        raise ValueError(f'voice reference not found: {candidate}')
    return candidate


def synthesize_chatterbox(text: str, voice: str) -> np.ndarray | None:
    """Synthesize English-only audio in the cloned voice. Returns float32 PCM
    at SAMPLE_RATE; resamples if the model emits a different rate."""
    if JAPANESE_PATTERN.search(text):
        raise ValueError('Chatterbox is English-only; use engine=kokoro for Japanese text')

    ref_wav = resolve_voice_ref(voice)
    model = get_chatterbox()
    wav = model.generate(text, audio_prompt_path=str(ref_wav))
    if hasattr(wav, 'cpu'):
        wav = wav.cpu().numpy()
    audio = np.asarray(wav, dtype=np.float32).squeeze()
    if audio.size == 0:
        return None

    src_sr = int(getattr(model, 'sr', SAMPLE_RATE))
    if src_sr != SAMPLE_RATE:
        from scipy.signal import resample_poly
        audio = resample_poly(audio, SAMPLE_RATE, src_sr).astype(np.float32)
    return audio


def synthesize(text, en_pipeline, jp_pipeline, en_voice, jp_voice, switch_gap_ms=60):
    """Synthesize a sentence, returning a float32 numpy array (or None)."""
    segments = segment_by_language(text)
    if not segments:
        return None

    gap_samples = int(SAMPLE_RATE * switch_gap_ms / 1000)
    inter_gap = np.zeros(gap_samples, dtype=np.float32) if gap_samples > 0 else None

    all_chunks = []
    for idx, (lang, seg_text) in enumerate(segments):
        if not seg_text.strip() or not re.search(r'\w', seg_text):
            continue

        if lang == 'JP':
            pipeline, voice = jp_pipeline, jp_voice
        else:
            pipeline, voice = en_pipeline, en_voice

        for _, _, audio in pipeline(seg_text, voice=voice):
            if hasattr(audio, 'cpu'):
                audio = audio.cpu().numpy()
            all_chunks.append(np.asarray(audio, dtype=np.float32))

        if inter_gap is not None and idx < len(segments) - 1:
            all_chunks.append(inter_gap)

    if not all_chunks:
        return None
    return np.concatenate(all_chunks)


def send_json(conn, obj):
    conn.sendall(json.dumps(obj).encode() + b'\n')


def recv_line(rfile):
    line = rfile.readline()
    if not line:
        return None
    return line.decode().strip()


def handle_connection(conn, en_pipeline, jp_pipeline):
    global last_activity
    rfile = conn.makefile('rb')
    try:
        while True:
            line = recv_line(rfile)
            if not line:
                break
            last_activity = time.monotonic()
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                send_json(conn, {'status': 'error', 'message': 'invalid JSON'})
                continue

            if 'command' in request:
                cmd = request['command']
                if cmd == 'ping':
                    send_json(conn, {'status': 'ok', 'message': 'pong'})
                elif cmd == 'shutdown':
                    send_json(conn, {'status': 'ok', 'message': 'shutting down'})
                    return 'shutdown'
                else:
                    send_json(conn, {'status': 'error', 'message': f'unknown command: {cmd}'})
                continue

            text = request.get('text', '')
            engine = request.get('engine', 'kokoro')
            en_voice = request.get('en_voice', 'af_heart')
            jp_voice = request.get('jp_voice', 'jf_alpha')
            switch_gap_ms = request.get('switch_gap_ms', 60)
            voice = request.get('voice')

            try:
                if engine == 'chatterbox':
                    audio = synthesize_chatterbox(text, voice)
                elif engine == 'kokoro':
                    audio = synthesize(text, en_pipeline, jp_pipeline, en_voice, jp_voice, switch_gap_ms)
                else:
                    raise ValueError(f'unknown engine: {engine}')
            except Exception as exc:
                send_json(conn, {'status': 'error', 'message': str(exc)})
                continue

            if audio is None:
                send_json(conn, {'status': 'ok', 'sample_rate': SAMPLE_RATE, 'num_samples': 0, 'byte_length': 0})
                continue

            audio_bytes = audio.tobytes()
            send_json(conn, {
                'status': 'ok',
                'sample_rate': SAMPLE_RATE,
                'num_samples': len(audio),
                'byte_length': len(audio_bytes),
            })
            conn.sendall(audio_bytes)
            last_activity = time.monotonic()
    finally:
        rfile.close()
    return None


def start_idle_watchdog(idle_timeout):
    """Spawn a daemon thread that SIGTERMs the process after idle_timeout seconds."""
    if idle_timeout <= 0:
        return

    def _watch():
        check_interval = min(30, max(5, idle_timeout // 4))
        while True:
            time.sleep(check_interval)
            if time.monotonic() - last_activity > idle_timeout:
                print(f'Idle for {idle_timeout}s, shutting down.', flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    t = threading.Thread(target=_watch, name='idle-watchdog', daemon=True)
    t.start()


def cleanup(sock_path, pid_path):
    for path in (sock_path, pid_path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Long-running Kokoro TTS server.')
    parser.add_argument('--socket', default=DEFAULT_SOCKET, help=f'Unix socket path (default: {DEFAULT_SOCKET})')
    parser.add_argument('--pid', default=DEFAULT_PID, help=f'PID file path (default: {DEFAULT_PID})')
    parser.add_argument('--idle-timeout', type=int, default=DEFAULT_IDLE_TIMEOUT,
                        help=f'Auto-shutdown after N seconds idle, 0 to disable (default: {DEFAULT_IDLE_TIMEOUT})')
    args = parser.parse_args()

    en_pipeline, jp_pipeline = load_pipelines()

    # Clean up stale socket
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    # Write PID file
    with open(args.pid, 'w') as f:
        f.write(str(os.getpid()))

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    srv.listen(1)

    def handle_signal(signum, frame):
        print(f'\nReceived signal {signum}, shutting down...', flush=True)
        srv.close()
        cleanup(args.socket, args.pid)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    global last_activity
    last_activity = time.monotonic()
    start_idle_watchdog(args.idle_timeout)

    timeout_note = f'idle-timeout {args.idle_timeout}s' if args.idle_timeout > 0 else 'no idle timeout'
    print(f'TTS server ready on {args.socket} (PID {os.getpid()}, {timeout_note})', flush=True)

    try:
        while True:
            conn, _ = srv.accept()
            try:
                result = handle_connection(conn, en_pipeline, jp_pipeline)
                if result == 'shutdown':
                    break
            except BrokenPipeError:
                pass
            finally:
                conn.close()
    finally:
        srv.close()
        cleanup(args.socket, args.pid)

    print('Server stopped.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
