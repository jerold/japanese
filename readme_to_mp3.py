#!/usr/bin/env python3
"""
readme_to_mp3.py — Convert a markdown/text file with mixed Japanese and English
content into per-sentence MP3 files for language learning.

Each sentence becomes one MP3. Within a sentence, the script segments the text
by script (hiragana/katakana/kanji vs. Latin letters) and switches voices mid-
sentence — so a line like "The word 犬 means dog" is synthesized as three
consecutive clips stitched together, with "犬" spoken by the Japanese voice and
everything else by the English voice. Digits, punctuation, and whitespace
attach to the preceding segment.

USAGE
-----
    python readme_to_mp3.py input.md output_dir/
    python readme_to_mp3.py input.md output_dir/ --combined
    python readme_to_mp3.py input.md output_dir/ --en-voice am_michael --jp-voice jm_kumo

DEPENDENCIES
------------
    pip install "kokoro>=0.9.4" soundfile pydub numpy

System packages:
    - espeak-ng   (required by Kokoro for English phonemization)
    - ffmpeg      (required by pydub for MP3 export)

For Japanese, Kokoro will pull in pyopenjtalk / fugashi automatically on first
run; if you hit an import error, run:
    pip install pyopenjtalk fugashi[unidic-lite]

VOICES
------
English (American): af_heart, af_bella, af_nicole, am_michael, am_puck, am_adam
English (British):  bf_alice, bf_emma, bm_daniel, bm_george
Japanese female:    jf_alpha, jf_gongitsune, jf_nezumi, jf_tebukuro
Japanese male:      jm_kumo

See https://huggingface.co/hexgrad/Kokoro-82M for the full list.
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import re
import socket
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf
from pydub import AudioSegment

if TYPE_CHECKING:
    from kokoro import KPipeline

# ---------------------------------------------------------------------------
# Text parsing
# ---------------------------------------------------------------------------

# Unicode ranges covering Japanese scripts.
JAPANESE_PATTERN = re.compile(
    r'['
    r'\u3040-\u309F'   # Hiragana
    r'\u30A0-\u30FF'   # Katakana
    r'\u4E00-\u9FFF'   # CJK Unified Ideographs (Kanji)
    r'\u3400-\u4DBF'   # CJK Extension A
    r']'
)

# Sentence-end candidate: terminating punctuation + optional closing quote/paren + whitespace.
# We don't use this as a naive .split() — we walk matches and skip abbreviations.
_SENT_BOUNDARY = re.compile(r'([。！？.!?])(["\'\)\]]?)\s+')

# Words ending with a period that should NOT trigger a sentence break.
_ABBREVIATIONS = frozenset({
    'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'st',
    'vol', 'no', 'pp', 'fig', 'eq', 'cf', 'vs',
    'etc', 'inc', 'co', 'ltd',
})

# Paragraph break: two or more newlines (possibly with whitespace between).
_PARAGRAPH_BREAK = re.compile(r'\n\s*\n+')

# Any whitespace run, for normalizing intra-paragraph text.
_WHITESPACE_RUN = re.compile(r'\s+')

# Default max chars for one TTS call in paragraph mode; longer paragraphs fall
# back to sentence splitting within the paragraph.
DEFAULT_MAX_PARAGRAPH_CHARS = 800


LATIN_LETTER = re.compile(r'[A-Za-z]')


def is_japanese(text: str) -> bool:
    """True if the text contains any Japanese character."""
    return bool(JAPANESE_PATTERN.search(text))


def classify_char(ch: str) -> str:
    """Classify a character as 'JP', 'EN', or 'NEUTRAL' (digits/punct/space)."""
    if JAPANESE_PATTERN.match(ch):
        return 'JP'
    if LATIN_LETTER.match(ch):
        return 'EN'
    return 'NEUTRAL'


def segment_by_language(text: str) -> list[tuple[str, str]]:
    """
    Break text into consecutive (lang, substring) runs where lang is 'JP' or 'EN'.

    Neutral characters (digits, punctuation, spaces) are attached to the run
    that immediately precedes them. A run with no language-tagged characters at
    all defaults to 'EN'.

    Example:
        "The word 犬 means dog." ->
            [('EN', 'The word '), ('JP', '犬 '), ('EN', 'means dog.')]
    """
    if not text:
        return []

    segments: list[tuple[str, str]] = []
    current_lang: str | None = None
    current_chars: list[str] = []

    for ch in text:
        cls = classify_char(ch)
        if cls == 'NEUTRAL':
            current_chars.append(ch)
            continue
        if current_lang is None or cls == current_lang:
            current_lang = cls
            current_chars.append(ch)
        else:
            # Language switch: emit current run, start a new one.
            segments.append((current_lang, ''.join(current_chars)))
            current_lang = cls
            current_chars = [ch]

    if current_chars:
        segments.append((current_lang or 'EN', ''.join(current_chars)))
    return segments


def sentence_language_tag(sentence: str) -> str:
    """Return 'JP', 'EN', or 'MIX' for filename-tagging purposes."""
    has_jp = bool(JAPANESE_PATTERN.search(sentence))
    has_en = bool(LATIN_LETTER.search(sentence))
    if has_jp and has_en:
        return 'MIX'
    if has_jp:
        return 'JP'
    return 'EN'


def strip_markdown(text: str) -> str:
    """Remove common markdown syntax so it isn't read aloud."""
    text = re.sub(r'```[\s\S]*?```', '', text)          # fenced code blocks
    text = re.sub(r'`[^`]*`', '', text)                  # inline code
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)          # images
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text) # links -> link text
    text = re.sub(r'^\s*#+\s*', '', text, flags=re.MULTILINE)  # headers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)       # bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)           # italic
    text = re.sub(r'^\s*>\s*', '', text, flags=re.MULTILINE)   # blockquotes
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # bullets
    return text


def _is_abbreviation_at(text: str, period_idx: int) -> bool:
    """True if the period at text[period_idx] is part of an abbreviation."""
    if text[period_idx] != '.':
        return False
    # Walk back to find the word containing the period (alnum + embedded periods).
    j = period_idx - 1
    while j >= 0 and (text[j].isalnum() or text[j] == '.'):
        j -= 1
    word = text[j + 1:period_idx].lower()
    if not word:
        return False
    # Single-letter initial (e.g., "B. Franklin").
    word_no_dots = word.replace('.', '')
    if len(word_no_dots) == 1 and word_no_dots.isalpha():
        return True
    # Multi-letter abbreviation (Mr, Dr, etc.).
    if word in _ABBREVIATIONS or word_no_dots in _ABBREVIATIONS:
        return True
    # Internal periods (e.g., "U.S.A", "i.e", "e.g") — treat as abbreviation.
    if '.' in word:
        return True
    return False


def _split_line_into_sentences(line: str) -> list[str]:
    """Split a single line at sentence boundaries, respecting abbreviations."""
    sentences: list[str] = []
    last = 0
    for m in _SENT_BOUNDARY.finditer(line):
        period_idx = m.start()
        if _is_abbreviation_at(line, period_idx):
            continue
        # Sentence body includes the punctuation and the optional closing quote.
        boundary_end = period_idx + 1 + (1 if m.group(2) else 0)
        sentences.append(line[last:boundary_end])
        last = m.end()
    if last < len(line):
        sentences.append(line[last:])
    return sentences


def split_sentences(text: str) -> list[str]:
    """Split cleaned text into a flat list of sentences, preserving order."""
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in _split_line_into_sentences(line):
            part = part.strip()
            if part:
                sentences.append(part)
    return sentences


def split_paragraphs(text: str) -> list[str]:
    """
    Split text into paragraphs, collapsing intra-paragraph whitespace.

    A paragraph is a run of text bounded by blank lines (one or more). Single
    newlines inside a paragraph (e.g., source-level wrapping inside an HTML
    `<p>` tag) become spaces — they are not sentence boundaries.
    """
    paragraphs: list[str] = []
    for chunk in _PARAGRAPH_BREAK.split(text):
        cleaned = _WHITESPACE_RUN.sub(' ', chunk).strip()
        if cleaned:
            paragraphs.append(cleaned)
    return paragraphs


def iter_chunks(
    text: str,
    mode: str,
    max_paragraph_chars: int = DEFAULT_MAX_PARAGRAPH_CHARS,
) -> list[str]:
    """
    Produce TTS chunks for `text`:
      - 'sentence': one chunk per sentence (current behavior).
      - 'paragraph': one chunk per paragraph; if a paragraph exceeds
        max_paragraph_chars, fall back to sentence-split within that paragraph
        so each TTS call stays inside Kokoro's comfort zone.
    """
    if mode == 'sentence':
        return split_sentences(text)
    if mode == 'paragraph':
        chunks: list[str] = []
        for para in split_paragraphs(text):
            if len(para) <= max_paragraph_chars:
                chunks.append(para)
            else:
                sub = split_sentences(para)
                chunks.extend(sub if sub else [para])
        return chunks
    raise ValueError(f'unknown chunk mode: {mode!r}')


# ---------------------------------------------------------------------------
# EPUB support
# ---------------------------------------------------------------------------

_PARAGRAPH_TAGS = frozenset({
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr', 'blockquote',
})
_SOFT_BREAK_TAGS = frozenset({'br'})
_SKIP_TAGS = frozenset({'style', 'script', 'head', 'title', 'nav'})


class _HTMLTextExtractor(HTMLParser):
    """
    Extract visible text from HTML, preserving paragraph boundaries.

    Paragraph-level tags (`<p>`, `<div>`, headings, list items, table rows,
    `<blockquote>`) emit `\n\n` on close — a real paragraph break the downstream
    splitter can rely on. Soft line breaks (`<br>`) emit a single space, so a
    `<br/>` inside a paragraph stays inside the paragraph. Source-level
    whitespace inside text nodes is collapsed before append so wrapped HTML
    source doesn't fragment sentences.
    """

    def __init__(self):
        super().__init__()
        self.pieces: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _SOFT_BREAK_TAGS:
            self.pieces.append(' ')

    def handle_startendtag(self, tag, attrs):
        # Self-closing tags like <br/>.
        if tag in _SOFT_BREAK_TAGS:
            self.pieces.append(' ')

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if tag in _PARAGRAPH_TAGS:
            self.pieces.append('\n\n')

    def handle_data(self, data):
        if self._skip_depth:
            return
        cleaned = _WHITESPACE_RUN.sub(' ', data)
        if cleaned:
            self.pieces.append(cleaned)

    def get_text(self) -> str:
        return ''.join(self.pieces).strip()


def _html_to_text(html: str) -> str:
    """Convert an HTML string to plain text."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def extract_epub_chapters(epub_path: Path) -> list[tuple[str, str]]:
    """
    Extract chapters from an EPUB file in reading order.

    Returns a list of (chapter_label, plain_text) tuples.
    """
    chapters: list[tuple[str, str]] = []

    with zipfile.ZipFile(epub_path) as zf:
        # Find the OPF file via META-INF/container.xml.
        container = ET.fromstring(zf.read('META-INF/container.xml'))
        ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
        opf_path = container.find('.//c:rootfile', ns).attrib['full-path']
        opf_dir = str(Path(opf_path).parent)

        opf = ET.fromstring(zf.read(opf_path))
        opf_ns = {'opf': 'http://www.idpf.org/2007/opf'}

        # Build id -> href map from manifest.
        manifest = {}
        for item in opf.findall('.//opf:manifest/opf:item', opf_ns):
            manifest[item.attrib['id']] = item.attrib['href']

        # Walk the spine for reading order.
        for itemref in opf.findall('.//opf:spine/opf:itemref', opf_ns):
            idref = itemref.attrib['idref']
            href = manifest.get(idref, '')
            if not href:
                continue

            full_path = f'{opf_dir}/{href}' if opf_dir != '.' else href
            try:
                html = zf.read(full_path).decode('utf-8')
            except KeyError:
                continue

            text = _html_to_text(html)
            if not text or len(text.split()) < 5:
                continue

            # Find a meaningful label from the first non-boilerplate line.
            label = f'part_{len(chapters)+1:02d}'
            for line in text.split('\n'):
                line = line.strip()
                if line and len(line) > 3 and not line.startswith('The Project Gutenberg'):
                    label = safe_filename(line, max_len=50)
                    break
            chapters.append((label, text))

    return chapters


def safe_filename(text: str, max_len: int = 40) -> str:
    """Turn a sentence into a filesystem-safe filename fragment."""
    cleaned = re.sub(
        r'[^\w\s\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]',
        '',
        text,
    )
    cleaned = re.sub(r'\s+', '_', cleaned).strip('_')
    return cleaned[:max_len] or 'sentence'


def _chapter_filename(ch_idx: int, ch_label: str, audio_ext: str) -> str:
    """Build the deterministic output filename for a chapter."""
    return f'ch{ch_idx:02d}_{ch_label}{audio_ext}'


# ---------------------------------------------------------------------------
# Audio generation
# ---------------------------------------------------------------------------

SAMPLE_RATE = 24000  # Kokoro outputs 24kHz audio


def generate_wav_bytes(
    sentence: str,
    en_pipeline: KPipeline,
    jp_pipeline: KPipeline,
    en_voice: str,
    jp_voice: str,
    inter_segment_gap_ms: int = 60,
) -> bytes | None:
    """
    Synthesize one sentence, switching voices mid-sentence when the script
    changes between Japanese and Latin. Returns in-memory WAV bytes.
    """
    segments = segment_by_language(sentence)
    if not segments:
        return None

    gap_samples = int(SAMPLE_RATE * inter_segment_gap_ms / 1000)
    inter_gap = np.zeros(gap_samples, dtype=np.float32) if gap_samples > 0 else None

    all_chunks: list[np.ndarray] = []
    for idx, (lang, text) in enumerate(segments):
        # Skip segments that are only whitespace / lone punctuation.
        if not text.strip() or not re.search(r'\w', text):
            continue

        if lang == 'JP':
            pipeline, voice = jp_pipeline, jp_voice
        else:
            pipeline, voice = en_pipeline, en_voice

        for _, _, audio in pipeline(text, voice=voice):
            # Kokoro may return a torch tensor; normalize to 1-D float32 numpy.
            if hasattr(audio, 'cpu'):
                audio = audio.cpu().numpy()
            all_chunks.append(np.asarray(audio, dtype=np.float32))

        # Small gap between segments to make the language switch feel natural.
        if inter_gap is not None and idx < len(segments) - 1:
            all_chunks.append(inter_gap)

    if not all_chunks:
        return None

    combined = np.concatenate(all_chunks)
    buf = io.BytesIO()
    sf.write(buf, combined, SAMPLE_RATE, format='WAV')
    buf.seek(0)
    return buf.read()


def _export_audio(segment: AudioSegment, path: Path, audio_format: str = 'aac') -> None:
    """Export an AudioSegment to disk in the given format."""
    if audio_format == 'aac':
        segment = segment.set_channels(1)  # mono
        segment.export(path, format='adts', codec='aac', bitrate='64k')
    else:
        segment.export(path, format='mp3', bitrate='128k')


def wav_bytes_to_mp3(wav_bytes: bytes, mp3_path: Path) -> AudioSegment:
    """Write WAV bytes as MP3 and return the AudioSegment for optional reuse."""
    segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    segment.export(mp3_path, format='mp3', bitrate='128k')
    return segment


# ---------------------------------------------------------------------------
# TTS server client
# ---------------------------------------------------------------------------

DEFAULT_SOCKET = '/tmp/kokoro-tts.sock'


class ServerConnection:
    """Persistent connection to the TTS server with buffered reading."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.rfile = sock.makefile('rb')

    @staticmethod
    def connect(sock_path: str = DEFAULT_SOCKET) -> ServerConnection | None:
        """Try to connect to a running TTS server. Returns connection or None."""
        if not Path(sock_path).exists():
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sock_path)
            return ServerConnection(sock)
        except (ConnectionRefusedError, FileNotFoundError):
            return None

    def close(self):
        self.rfile.close()
        self.sock.close()

    def generate_wav_bytes(
        self,
        sentence: str,
        en_voice: str,
        jp_voice: str,
        switch_gap_ms: int,
    ) -> bytes | None:
        """Send a TTS request to the server, return WAV bytes."""
        request = json.dumps({
            'text': sentence,
            'en_voice': en_voice,
            'jp_voice': jp_voice,
            'switch_gap_ms': switch_gap_ms,
        })
        self.sock.sendall(request.encode() + b'\n')

        header_line = self.rfile.readline()
        header = json.loads(header_line)

        if header.get('status') != 'ok':
            raise RuntimeError(header.get('message', 'server error'))

        byte_length = header['byte_length']
        if byte_length == 0:
            return None

        raw = self.rfile.read(byte_length)
        if len(raw) < byte_length:
            raise ConnectionError('Server closed connection')
        audio = np.frombuffer(raw, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, audio, SAMPLE_RATE, format='WAV')
        buf.seek(0)
        return buf.read()


# ---------------------------------------------------------------------------
# Parallel processing
# ---------------------------------------------------------------------------

# Module-level globals for worker processes (set by _init_worker).
_worker_en_pipeline = None
_worker_jp_pipeline = None


def _get_total_ram_gb() -> float:
    """Return total physical RAM in GB."""
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024 ** 3)
    except (AttributeError, ValueError):
        pass
    # macOS fallback
    try:
        out = subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True)
        return int(out.strip()) / (1024 ** 3)
    except Exception:
        return 8.0  # conservative default


def _detect_worker_count(pipeline_memory_gb: float = 4.0, reserve_gb: float = 4.0) -> int:
    """Auto-detect how many parallel workers can fit in RAM."""
    total_gb = _get_total_ram_gb()
    available = total_gb - reserve_gb
    max_by_ram = int(available / pipeline_memory_gb)
    max_by_cpu = (os.cpu_count() or 2) // 2
    workers = max(1, min(max_by_ram, max_by_cpu))
    return workers


def _init_worker():
    """Pool initializer: load pipelines once per worker process."""
    global _worker_en_pipeline, _worker_jp_pipeline
    from kokoro import KPipeline
    _worker_en_pipeline = KPipeline(lang_code='a')
    _worker_jp_pipeline = KPipeline(lang_code='j')


def _process_chapter(args_tuple):
    """Process a single chapter in a worker. Returns result tuple."""
    (ch_idx, ch_label, ch_text, output_dir,
     en_voice, jp_voice, switch_gap_ms, gap_ms,
     audio_format, audio_ext, chunk_mode, max_paragraph_chars) = args_tuple

    chunks = iter_chunks(ch_text, chunk_mode, max_paragraph_chars)
    if not chunks:
        return (ch_idx, None, None, 0.0, 0, 0)

    gap = AudioSegment.silent(duration=gap_ms)
    ch_audio = AudioSegment.silent(duration=0)
    errors = 0

    for chunk in chunks:
        try:
            wav_bytes = generate_wav_bytes(
                chunk, _worker_en_pipeline, _worker_jp_pipeline,
                en_voice, jp_voice, inter_segment_gap_ms=switch_gap_ms,
            )
            if wav_bytes is None:
                continue
            ch_audio += AudioSegment.from_wav(io.BytesIO(wav_bytes)) + gap
        except Exception:
            errors += 1

    if len(ch_audio) == 0:
        return (ch_idx, None, None, 0.0, len(chunks), errors)

    ch_filename = _chapter_filename(ch_idx, ch_label, audio_ext)
    ch_path = Path(output_dir) / ch_filename
    _export_audio(ch_audio, ch_path, audio_format)
    duration_min = len(ch_audio) / 60000

    return (ch_idx, ch_filename, str(ch_path), duration_min, len(chunks), errors)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _resolve_input(raw_input: str) -> tuple[str, bool, bool]:
    """Determine if input is a file/lesson reference or raw text.

    Returns (text_or_path, is_file, is_epub).
    For EPUB files, text_or_path is the file path (chapters are extracted later).
    """
    stripped = raw_input.strip()

    # Check for day/lesson shorthand like "1", "01", "day-01"
    lesson_match = re.match(r'^(?:day-?)?(\d{1,2})$', stripped, re.IGNORECASE)
    if lesson_match:
        day_num = int(lesson_match.group(1))
        path = Path(f'day-{day_num:02d}.md')
        if path.exists():
            return path.read_text(encoding='utf-8'), True, False

    # Check if it's a file path
    path = Path(stripped)
    if path.is_file():
        if path.suffix.lower() == '.epub':
            return stripped, True, True
        return path.read_text(encoding='utf-8'), True, False

    # Treat as raw text
    return stripped, False, False


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Convert text, a file, or a lesson number into MP3 audio.',
        epilog='Examples:\n'
               '  %(prog)s "おはようございます" --play\n'
               '  %(prog)s 1                        # day-01.md\n'
               '  %(prog)s day-05.md output/day-05/\n'
               '  %(prog)s input.md output/ --combined\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('input', help='Text string, file path, EPUB, or lesson number (e.g. "1" for day-01.md)')
    parser.add_argument('output_dir', type=Path, nargs='?', default=None,
                        help='Directory for output MP3s (default: output/day-NN/ for lessons, /tmp/ for text)')
    parser.add_argument('--play', action='store_true',
                        help='Play the audio on speakers after generating')
    parser.add_argument('--en-voice', default='af_heart',
                        help='English Kokoro voice (default: af_heart)')
    parser.add_argument('--jp-voice', default='jf_alpha',
                        help='Japanese Kokoro voice (default: jf_alpha)')
    parser.add_argument('--combined', action='store_true',
                        help='Also produce one combined.mp3 with all sentences back-to-back')
    parser.add_argument('--gap-ms', type=int, default=150,
                        help='Silence between sentences (default: 150ms)')
    parser.add_argument('--switch-gap-ms', type=int, default=60,
                        help='Silence between language-switched segments within a sentence (default: 60ms)')
    parser.add_argument('--no-server', action='store_true',
                        help='Skip TTS server and load pipelines locally')
    parser.add_argument('--format', choices=['mp3', 'aac'], default='aac',
                        help='Audio format: aac (64k mono, small) or mp3 (128k stereo) (default: aac)')
    parser.add_argument('--workers', type=int, default=0,
                        help='Parallel workers for EPUB (default: auto-detect based on RAM)')
    parser.add_argument('--chunk', choices=['sentence', 'paragraph', 'auto'], default='auto',
                        help='TTS chunk size: paragraph for fluid audiobook listening, '
                             'sentence for per-line study clips, auto picks paragraph for '
                             'EPUB and sentence elsewhere (default: auto)')
    parser.add_argument('--max-paragraph-chars', type=int, default=DEFAULT_MAX_PARAGRAPH_CHARS,
                        help=f'In paragraph mode, paragraphs longer than this fall back to '
                             f'sentence splitting (default: {DEFAULT_MAX_PARAGRAPH_CHARS})')
    args = parser.parse_args()

    audio_ext = '.aac' if args.format == 'aac' else '.mp3'

    raw, is_file, is_epub = _resolve_input(args.input)

    # Resolve auto chunk mode: paragraph for EPUB, sentence for everything else.
    if args.chunk == 'auto':
        chunk_mode = 'paragraph' if is_epub else 'sentence'
    else:
        chunk_mode = args.chunk

    # For raw text input, default to /tmp and auto-play.
    if not is_file and args.output_dir is None:
        args.output_dir = Path('/tmp/tts-output')
        args.play = True
    elif args.output_dir is None:
        # Derive from input path: day-01.md -> output/day-01/
        input_path = Path(args.input)
        args.output_dir = Path('output') / input_path.stem

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Try connecting to the TTS server for faster synthesis.
    server = None if args.no_server else ServerConnection.connect()
    en_pipeline = jp_pipeline = None

    if server:
        print('Using TTS server (pipelines pre-loaded).')
    else:
        from kokoro import KPipeline
        print('Loading Kokoro pipelines (first run downloads ~300MB of weights)...')
        en_pipeline = KPipeline(lang_code='a')  # 'a' = American English
        jp_pipeline = KPipeline(lang_code='j')  # 'j' = Japanese

    def _synth_sentence(sentence: str) -> bytes | None:
        """Synthesize a single sentence via server or local pipeline."""
        if server:
            return server.generate_wav_bytes(
                sentence, args.en_voice, args.jp_voice, args.switch_gap_ms,
            )
        return generate_wav_bytes(
            sentence, en_pipeline, jp_pipeline,
            args.en_voice, args.jp_voice,
            inter_segment_gap_ms=args.switch_gap_ms,
        )

    # --- EPUB: one combined audio file per chapter ---
    if is_epub:
        chapters = extract_epub_chapters(Path(raw))
        if not chapters:
            print('No chapters found in EPUB.', file=sys.stderr)
            return 1

        chunk_kind = 'paragraph' if chunk_mode == 'paragraph' else 'sentence'
        total_chunks = 0
        for _, text in chapters:
            total_chunks += len(iter_chunks(text, chunk_mode, args.max_paragraph_chars))

        # Determine worker count.
        workers = args.workers if args.workers > 0 else _detect_worker_count()
        # Can't use multiprocessing with the TTS server (socket isn't shareable).
        if server:
            workers = 1

        total_ram = _get_total_ram_gb()
        print(f'EPUB: {len(chapters)} chapter(s), ~{total_chunks} {chunk_kind}(s) total.')
        print(f'Format: {args.format} ({("64k mono" if args.format == "aac" else "128k stereo")})')
        print(f'Chunking: {chunk_kind} (max {args.max_paragraph_chars} chars per paragraph)' if chunk_mode == 'paragraph'
              else f'Chunking: {chunk_kind}')
        print(f'Workers: {workers} (~{workers * 4}GB pipelines, {total_ram:.0f}GB total RAM)\n')

        last_out = None

        # Check which chapters already have output files and can be skipped.
        existing_chapters: dict[int, Path] = {}  # ch_idx -> path
        for ch_idx, (ch_label, _ch_text) in enumerate(chapters, start=1):
            ch_path = args.output_dir / _chapter_filename(ch_idx, ch_label, audio_ext)
            if ch_path.exists():
                existing_chapters[ch_idx] = ch_path

        if existing_chapters:
            print(f'Resuming: skipping {len(existing_chapters)} existing chapter(s), '
                  f'generating {len(chapters) - len(existing_chapters)} remaining.\n')

        # --- Parallel path (workers > 1, no server) ---
        if workers > 1 and not server:
            chapter_args = [
                (ch_idx, ch_label, ch_text, str(args.output_dir),
                 args.en_voice, args.jp_voice, args.switch_gap_ms,
                 args.gap_ms, args.format, audio_ext,
                 chunk_mode, args.max_paragraph_chars)
                for ch_idx, (ch_label, ch_text) in enumerate(chapters, start=1)
                if ch_idx not in existing_chapters
            ]

            # Collect results from existing files for --combined.
            results = [
                (ch_idx, ch_path.name, str(ch_path), 0.0, 0, 0)
                for ch_idx, ch_path in existing_chapters.items()
            ]
            for ch_idx, ch_path in sorted(existing_chapters.items()):
                print(f'  Chapter {ch_idx:2d}: {ch_path.name} (exists, skipped)')
                last_out = ch_path

            if chapter_args:
                print(f'Launching {workers} workers (each loading pipelines — may take a few minutes)...\n')
                completed = 0

                with multiprocessing.Pool(processes=workers, initializer=_init_worker) as pool:
                    for result in pool.imap_unordered(_process_chapter, chapter_args):
                        ch_idx, ch_filename, ch_path, duration_min, chunk_count, err_count = result
                        completed += 1
                        if ch_filename:
                            err_note = f' ({err_count} errors)' if err_count else ''
                            print(f'  [{completed:2d}/{len(chapter_args)}] Chapter {ch_idx:2d}: '
                                  f'{ch_filename} ({duration_min:.1f} min, {chunk_count} {chunk_kind}s){err_note}')
                            last_out = Path(ch_path)
                            results.append(result)
                        else:
                            print(f'  [{completed:2d}/{len(chapter_args)}] Chapter {ch_idx:2d}: SKIP')

            # Build combined file from chapter files if requested.
            if args.combined and results:
                print('\nBuilding combined audiobook...')
                results.sort(key=lambda r: r[0])  # sort by chapter index
                book_combined = AudioSegment.silent(duration=0)
                for _, _, ch_path, _, _, _ in results:
                    book_combined += AudioSegment.from_file(ch_path)
                combined_path = args.output_dir / f'combined{audio_ext}'
                _export_audio(book_combined, combined_path, args.format)
                last_out = combined_path
                print(f'Full audiobook: {combined_path} ({len(book_combined)/60000:.1f} min)')

        # --- Sequential path (1 worker or server) ---
        else:
            gap = AudioSegment.silent(duration=args.gap_ms)
            book_combined = AudioSegment.silent(duration=0) if args.combined else None
            chunk_num = 0

            for ch_idx, (ch_label, ch_text) in enumerate(chapters, start=1):
                ch_filename = _chapter_filename(ch_idx, ch_label, audio_ext)
                ch_path = args.output_dir / ch_filename

                # Skip chapters that already have output files.
                if ch_idx in existing_chapters:
                    print(f'  Chapter {ch_idx:2d}: {ch_filename} (exists, skipped)')
                    last_out = ch_path
                    if book_combined is not None:
                        book_combined += AudioSegment.from_file(ch_path)
                    continue

                chunks = iter_chunks(ch_text, chunk_mode, args.max_paragraph_chars)
                if not chunks:
                    print(f'  Chapter {ch_idx:2d}: SKIP (no {chunk_kind}s)')
                    continue

                ch_audio = AudioSegment.silent(duration=0)
                ch_errors = 0

                for i, chunk in enumerate(chunks, start=1):
                    chunk_num += 1
                    try:
                        wav_bytes = _synth_sentence(chunk)
                        if wav_bytes is None:
                            continue
                        ch_audio += AudioSegment.from_wav(io.BytesIO(wav_bytes)) + gap
                    except Exception as exc:
                        ch_errors += 1
                        print(f'    [{chunk_num}/{total_chunks}] ERROR: {exc}', file=sys.stderr)

                    if i % 50 == 0 or i == len(chunks):
                        print(f'    [{chunk_num:5d}/{total_chunks}] ch{ch_idx:02d} {chunk_kind} {i}/{len(chunks)}')

                if len(ch_audio) > 0:
                    _export_audio(ch_audio, ch_path, args.format)
                    last_out = ch_path
                    duration_min = len(ch_audio) / 60000
                    err_note = f' ({ch_errors} errors)' if ch_errors else ''
                    print(f'  Chapter {ch_idx:2d}: {ch_filename} ({duration_min:.1f} min, {len(chunks)} {chunk_kind}s){err_note}')

                    if book_combined is not None:
                        book_combined += ch_audio

            if book_combined is not None and len(book_combined) > 0:
                combined_path = args.output_dir / f'combined{audio_ext}'
                _export_audio(book_combined, combined_path, args.format)
                last_out = combined_path
                print(f'\nFull audiobook: {combined_path} ({len(book_combined)/60000:.1f} min)')

    # --- Regular file or raw text: per-sentence files ---
    else:
        source_text = strip_markdown(raw) if is_file else raw
        sentences = iter_chunks(source_text, chunk_mode, args.max_paragraph_chars)

        if not sentences:
            print('No sentences found after parsing.', file=sys.stderr)
            return 1

        print(f'Found {len(sentences)} {("paragraph" if chunk_mode == "paragraph" else "sentence")}(s). Generating audio...\n')

        combined = AudioSegment.silent(duration=0) if args.combined or not is_file else None
        gap = AudioSegment.silent(duration=args.gap_ms)

        last_out = None
        for i, sentence in enumerate(sentences, start=1):
            lang = sentence_language_tag(sentence)
            filename = f'{i:03d}_{lang}_{safe_filename(sentence)}{audio_ext}'
            out_path = args.output_dir / filename

            try:
                wav_bytes = _synth_sentence(sentence)
                if wav_bytes is None:
                    print(f'  [{i:3d}/{len(sentences)}] SKIP (no audio): {sentence[:60]}')
                    continue

                segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
                _export_audio(segment, out_path, args.format)
                last_out = out_path
                print(f'  [{i:3d}/{len(sentences)}] {lang}: {sentence[:60]}')

                if combined is not None:
                    combined += segment + gap

            except Exception as exc:
                print(f'  [{i:3d}/{len(sentences)}] ERROR: {exc}', file=sys.stderr)

        if combined is not None and len(combined) > 0:
            combined_path = args.output_dir / f'combined{audio_ext}'
            _export_audio(combined, combined_path, args.format)
            last_out = combined_path
            print(f'\nCombined track: {combined_path}')

    if server:
        server.close()

    print(f'\nDone. Output directory: {args.output_dir}')

    if args.play and last_out:
        print(f'Playing {last_out}...')
        subprocess.run(['afplay', str(last_out)])

    return 0


if __name__ == '__main__':
    sys.exit(main())
