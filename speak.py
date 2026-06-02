#!/usr/bin/env python3
"""
speak.py — Synthesize text via the Kokoro TTS server and play it.

The thin client paired with tts_server.py. Designed to be invoked as `speak`
through the ~/.local/bin/speak shim, which activates the project venv.

Text comes from positional arguments when given, otherwise from stdin.

USAGE
-----
    speak "Hello 犬"
    speak --jp-voice jm_kumo "おはようございます"
    echo "Hello 犬" | speak
    cat passage.txt | speak --bg

EXIT CODES
----------
    0   played (or queued, with --bg)
    2   no input (no args and stdin was a TTY)
    3   TTS server could not be reached or auto-started
    4   server returned an error
    5   afplay failed
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SOCKET_PATH = '/tmp/kokoro-tts.sock'
SERVER_LOG = '/tmp/kokoro-tts.log'
SAMPLE_RATE = 24000
STARTUP_TIMEOUT = 30.0
PROJECT_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = PROJECT_DIR / 'tts_server.py'


def server_alive(sock_path: str = SOCKET_PATH) -> bool:
    """Return True iff a server answers ping on the socket."""
    if not Path(sock_path).exists():
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(sock_path)
        sock.sendall(b'{"command": "ping"}\n')
        reply = sock.makefile('rb').readline()
        sock.close()
        return json.loads(reply).get('status') == 'ok'
    except (OSError, json.JSONDecodeError):
        return False


def launch_server() -> None:
    """Spawn tts_server.py detached. Does not wait for readiness."""
    # Clean up a stale socket file (from a crashed previous run).
    try:
        if Path(SOCKET_PATH).exists():
            os.unlink(SOCKET_PATH)
    except OSError:
        pass

    log = open(SERVER_LOG, 'ab')
    subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=str(PROJECT_DIR),
    )


def wait_for_server(deadline: float) -> bool:
    """Poll until the server answers ping or deadline elapses."""
    while time.monotonic() < deadline:
        if server_alive():
            return True
        time.sleep(0.2)
    return False


def ensure_server() -> None:
    """Connect to a running server, or start one and wait for readiness."""
    if server_alive():
        return
    print('Starting TTS server (one-time ~5s warmup)...', file=sys.stderr, flush=True)
    launch_server()
    if not wait_for_server(time.monotonic() + STARTUP_TIMEOUT):
        sys.stderr.write(
            f'TTS server failed to start within {STARTUP_TIMEOUT:.0f}s. '
            f'Check {SERVER_LOG}.\n'
        )
        sys.exit(3)


def synthesize(text: str, en_voice: str, jp_voice: str, switch_gap_ms: int,
               engine: str = 'kokoro', voice: str | None = None) -> bytes:
    """Send a synthesis request, return raw float32 PCM bytes."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    try:
        payload = {
            'text': text,
            'en_voice': en_voice,
            'jp_voice': jp_voice,
            'switch_gap_ms': switch_gap_ms,
            'engine': engine,
        }
        if voice is not None:
            payload['voice'] = voice
        request = json.dumps(payload)
        sock.sendall(request.encode() + b'\n')

        rfile = sock.makefile('rb')
        header = json.loads(rfile.readline())
        if header.get('status') != 'ok':
            sys.stderr.write(f'Server error: {header.get("message", "unknown")}\n')
            sys.exit(4)

        byte_length = header['byte_length']
        if byte_length == 0:
            return b''

        chunks = []
        remaining = byte_length
        while remaining > 0:
            chunk = rfile.read(remaining)
            if not chunk:
                sys.stderr.write('Server closed connection mid-stream.\n')
                sys.exit(4)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)
    finally:
        sock.close()


def write_wav(pcm_bytes: bytes) -> Path:
    """Write raw float32 PCM to a temp WAV file, return the path."""
    audio = np.frombuffer(pcm_bytes, dtype=np.float32)
    fd, path = tempfile.mkstemp(prefix='speak-', suffix='.wav', dir='/tmp')
    os.close(fd)
    sf.write(path, audio, SAMPLE_RATE, format='WAV')
    return Path(path)


def play(wav_path: Path, background: bool) -> int:
    """Hand off to afplay. Returns process exit code (0 for --bg)."""
    if background:
        subprocess.Popen(
            ['/bin/sh', '-c', f'afplay {wav_path!s}; rm -f {wav_path!s}'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0

    try:
        result = subprocess.run(['afplay', str(wav_path)])
        return result.returncode
    finally:
        try:
            wav_path.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Speak text using the Kokoro TTS server.',
        epilog='Examples: speak "Hello 犬"   |   echo "Hello 犬" | speak',
    )
    parser.add_argument('text', nargs='*',
                        help='Text to speak. If omitted, read from stdin.')
    parser.add_argument('--bg', action='store_true',
                        help='Play in background; exit immediately.')
    parser.add_argument('--en-voice', default='af_heart',
                        help='English Kokoro voice (default: af_heart)')
    parser.add_argument('--jp-voice', default='jf_alpha',
                        help='Japanese Kokoro voice (default: jf_alpha)')
    parser.add_argument('--switch-gap-ms', type=int, default=60,
                        help='Silence between language-switched segments (default: 60ms)')
    parser.add_argument('--engine', choices=['kokoro', 'chatterbox'], default='kokoro',
                        help='TTS engine (default: kokoro). Use chatterbox for cloned voices.')
    parser.add_argument('--voice', default=None,
                        help='For --engine chatterbox: path to a reference WAV or a '
                             'nickname registered in voices/registry.json')
    args = parser.parse_args()

    if args.engine == 'chatterbox' and not args.voice:
        parser.error('--engine chatterbox requires --voice (path or nickname)')

    if args.text:
        text = ' '.join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        sys.stderr.write(
            'speak: no input. Pass text as args (`speak "hello"`) '
            'or pipe via stdin (`echo hello | speak`).\n'
        )
        return 2

    if not text.strip():
        return 0

    ensure_server()
    pcm = synthesize(text, args.en_voice, args.jp_voice, args.switch_gap_ms,
                     engine=args.engine, voice=args.voice)
    if not pcm:
        return 0

    wav_path = write_wav(pcm)
    code = play(wav_path, background=args.bg)
    if code != 0:
        sys.stderr.write(f'afplay exited {code}\n')
        return 5
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
