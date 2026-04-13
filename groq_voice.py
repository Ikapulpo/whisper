#!/usr/bin/env python3
"""
Groq Voice Input — 音声入力 → Whisper文字起こし → LLM自動校正

Superwhisperの代替として、Groq APIを直接呼び出すPythonスクリプト。
勝間和代氏のワークフローを再現: 音声入力 → Whisper → Llama 3.3 70B校正
Zoom会議のシステム音声キャプチャにも対応。
"""

import argparse
import io
import os
import subprocess
import sys
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from groq import Groq
from pydub import AudioSegment
from pydub.silence import split_on_silence

# ── 定数 ──────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16000  # 16kHz — Whisperの学習レートと同じ
CHANNELS = 1  # モノラル
DTYPE = "int16"  # 16-bit PCM
MAX_CHUNK_SIZE_MB = 23  # Groqの25MB制限より余裕を持たせる
MAX_CHUNK_SIZE_BYTES = MAX_CHUNK_SIZE_MB * 1024 * 1024
WHISPER_MODEL = "whisper-large-v3-turbo"
LLM_MODEL = "llama-3.3-70b-versatile"
SILENCE_THRESH_DB = -40  # 無音検出の閾値 (dBFS)
MIN_SILENCE_LEN_MS = 700  # 無音と判定する最小長 (ms)

CORRECTION_SYSTEM_PROMPT = """\
あなたは日本語の音声認識テキストを校正する専門家です。

以下のルールに従って、音声認識で生成されたテキストを修正してください：

1. 誤認識された単語を文脈から正しい単語に修正する
2. 適切な句読点（。、！？）を追加する
3. 文の区切りを正しく判断し、改行を入れる
4. 明らかな繰り返しや言い淀み（「えー」「あの」「えっと」など）を除去する
5. 漢字の変換ミスを修正する
6. 固有名詞のスペルを文脈から推測して修正する
7. 元の意味やニュアンスは絶対に変えないこと
8. テキストの内容について説明やコメントは不要。修正後のテキストのみを出力すること。

修正後のテキストのみを出力してください。"""


# ── 音声デバイス検出 ──────────────────────────────────────────────────────────

def list_audio_sources():
    """利用可能な音声デバイスを一覧表示する。"""
    print("=== sounddevice デバイス一覧 ===")
    print(sd.query_devices())
    print()

    print("=== PulseAudio ソース一覧 ===")
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print("pactl の実行に失敗しました。PulseAudioが動作していない可能性があります。")
    except FileNotFoundError:
        print("pactl が見つかりません。pulseaudio-utils をインストールしてください。")
    except subprocess.TimeoutExpired:
        print("pactl がタイムアウトしました。")


def find_monitor_source() -> str:
    """PulseAudioモニターソースを自動検出する。"""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pactl が見つかりません。pulseaudio-utils をインストールしてください:\n"
            "  sudo apt install pulseaudio-utils"
        )

    for line in result.stdout.strip().split("\n"):
        if ".monitor" in line:
            return line.split("\t")[1]

    raise RuntimeError(
        "PulseAudioモニターソースが見つかりません。\n"
        "--list-devices で利用可能なソースを確認してください。"
    )


# ── レコーダー ────────────────────────────────────────────────────────────────

class MicRecorder:
    """マイクから録音する（sounddevice使用）。"""

    def __init__(self, device=None, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.device = device
        self.frames: list[np.ndarray] = []
        self.recording = False
        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[WARNING] {status}", file=sys.stderr)
        self.frames.append(indata.copy())

    def start(self):
        self.frames = []
        self.recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
        self.recording = False
        if not self.frames:
            return np.array([], dtype=np.int16)
        return np.concatenate(self.frames, axis=0).flatten()


class SystemAudioRecorder:
    """システム音声を録音する（PulseAudio parec使用）。"""

    def __init__(self, monitor_source=None, sample_rate=SAMPLE_RATE):
        self.monitor_source = monitor_source
        self.sample_rate = sample_rate
        self._process = None
        self._raw_data = bytearray()
        self._reader_thread = None

    def start(self):
        if not self.monitor_source:
            self.monitor_source = find_monitor_source()

        self._raw_data = bytearray()
        self._process = subprocess.Popen(
            [
                "parec",
                "--device", self.monitor_source,
                "--rate", str(self.sample_rate),
                "--channels", str(CHANNELS),
                "--format", "s16le",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

    def _read_output(self):
        while self._process and self._process.poll() is None:
            chunk = self._process.stdout.read(4096)
            if chunk:
                self._raw_data.extend(chunk)

    def stop(self) -> np.ndarray:
        if self._process:
            self._process.terminate()
            self._process.wait(timeout=5)
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
        if not self._raw_data:
            return np.array([], dtype=np.int16)
        return np.frombuffer(bytes(self._raw_data), dtype=np.int16)


class CombinedRecorder:
    """マイク＋システム音声を同時録音し、ミックスする。"""

    def __init__(self, mic_device=None, monitor_source=None):
        self.mic = MicRecorder(device=mic_device)
        self.system = SystemAudioRecorder(monitor_source=monitor_source)

    def start(self):
        self.system.start()
        self.mic.start()

    def stop(self) -> np.ndarray:
        mic_audio = self.mic.stop()
        sys_audio = self.system.stop()

        if len(mic_audio) == 0 and len(sys_audio) == 0:
            return np.array([], dtype=np.int16)
        if len(mic_audio) == 0:
            return sys_audio
        if len(sys_audio) == 0:
            return mic_audio

        # 長さを揃える（短い方をゼロ埋め）
        max_len = max(len(mic_audio), len(sys_audio))
        mic_padded = np.pad(mic_audio, (0, max_len - len(mic_audio)))
        sys_padded = np.pad(sys_audio, (0, max_len - len(sys_audio)))

        # ミックス: 加算して平均し、int16範囲にクリップ
        mixed = np.clip(
            (mic_padded.astype(np.int32) + sys_padded.astype(np.int32)) // 2,
            -32768, 32767,
        ).astype(np.int16)
        return mixed


# ── 音声処理 ──────────────────────────────────────────────────────────────────

def numpy_to_wav_bytes(audio_data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """numpy int16配列をWAVバイト列に変換する。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data.tobytes())
    return buf.getvalue()


def _export_wav_bytes(audio: AudioSegment) -> bytes:
    """AudioSegmentをWAVバイト列にエクスポートする。"""
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


def _force_split(audio: AudioSegment, segment_ms: int = 300_000) -> list[bytes]:
    """固定長（デフォルト5分）で強制分割する（2秒オーバーラップ）。"""
    overlap_ms = 2000
    result = []
    pos = 0
    while pos < len(audio):
        end = min(pos + segment_ms, len(audio))
        segment = audio[pos:end]
        result.append(_export_wav_bytes(segment))
        pos = end - overlap_ms
    return result


def chunk_audio_if_needed(wav_bytes: bytes) -> list[bytes]:
    """音声がGroq APIのサイズ制限を超える場合、チャンクに分割する。"""
    if len(wav_bytes) <= MAX_CHUNK_SIZE_BYTES:
        return [wav_bytes]

    audio = AudioSegment.from_wav(io.BytesIO(wav_bytes))

    # 無音ベースで分割を試みる
    chunks = split_on_silence(
        audio,
        min_silence_len=MIN_SILENCE_LEN_MS,
        silence_thresh=SILENCE_THRESH_DB,
        keep_silence=300,
    )

    if not chunks:
        return _force_split(audio)

    # 小さいチャンクを結合し、大きいチャンクは再分割
    result = []
    current = AudioSegment.empty()
    for chunk in chunks:
        test = current + chunk
        test_bytes = _export_wav_bytes(test)
        if len(test_bytes) > MAX_CHUNK_SIZE_BYTES:
            if len(current) > 0:
                result.append(_export_wav_bytes(current))
            chunk_bytes = _export_wav_bytes(chunk)
            if len(chunk_bytes) > MAX_CHUNK_SIZE_BYTES:
                result.extend(_force_split(chunk))
                current = AudioSegment.empty()
            else:
                current = chunk
        else:
            current = test

    if len(current) > 0:
        result.append(_export_wav_bytes(current))

    return result


# ── Groq API ──────────────────────────────────────────────────────────────────

def create_groq_client() -> Groq:
    """Groqクライアントを作成する。"""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("エラー: GROQ_API_KEY 環境変数が設定されていません。", file=sys.stderr)
        print("  export GROQ_API_KEY=your_api_key_here", file=sys.stderr)
        sys.exit(1)
    return Groq(api_key=api_key)


def transcribe_audio(client: Groq, wav_bytes: bytes) -> str:
    """音声をGroq Whisper APIで文字起こしする。"""
    chunks = chunk_audio_if_needed(wav_bytes)
    transcriptions = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  チャンク {i + 1}/{len(chunks)} を文字起こし中...")

        audio_file = io.BytesIO(chunk)
        audio_file.name = "recording.wav"

        transcription = client.audio.transcriptions.create(
            file=audio_file,
            model=WHISPER_MODEL,
            language="ja",
            response_format="verbose_json",
            temperature=0.0,
            prompt="日本語の音声認識です。",
        )
        transcriptions.append(transcription.text)

    return "\n".join(transcriptions)


def correct_text(client: Groq, raw_text: str) -> str:
    """文字起こしテキストをLlama 3.3 70Bで校正する。"""
    if not raw_text.strip():
        return ""

    completion = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": CORRECTION_SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
        temperature=0.3,
        max_tokens=4096,
    )

    return completion.choices[0].message.content


# ── 出力 ──────────────────────────────────────────────────────────────────────

def copy_to_clipboard(text: str) -> bool:
    """テキストをクリップボードにコピーする（xclip/xsel使用）。"""
    for cmd in [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def save_to_file(text: str, output_dir: str = "output") -> Path:
    """テキストをタイムスタンプ付きファイルに保存する。"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = Path(output_dir) / f"transcription_{timestamp}.txt"
    filepath.write_text(text, encoding="utf-8")
    return filepath


def display_results(
    raw_text: str,
    corrected_text: str,
    save: bool = True,
    clipboard: bool = True,
    output_dir: str = "output",
):
    """生テキストと校正済みテキストを表示し、各種出力を行う。"""
    print()
    print("=" * 60)
    print("[音声認識結果（生テキスト）]")
    print("-" * 60)
    print(raw_text)
    print()
    print("=" * 60)
    print("[修正後テキスト]")
    print("-" * 60)
    print(corrected_text)
    print("=" * 60)

    if clipboard:
        if copy_to_clipboard(corrected_text):
            print("-> クリップボードにコピーしました")
        else:
            print("-> クリップボードへのコピーに失敗（xclip/xsel をインストールしてください）")

    if save:
        filepath = save_to_file(corrected_text, output_dir)
        print(f"-> ファイルに保存しました: {filepath}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Groq Voice Input — 音声入力 -> Whisper文字起こし -> LLM校正",
    )
    parser.add_argument(
        "--mode", choices=["mic", "system", "both"], default="mic",
        help="音声ソース: mic=マイク, system=システム音声/Zoom, both=両方ミックス (default: mic)",
    )
    parser.add_argument(
        "--device", type=int, default=None,
        help="マイクデバイスのインデックス番号 (--list-devices で確認)",
    )
    parser.add_argument(
        "--monitor", type=str, default=None,
        help="PulseAudioモニターソース名 (未指定なら自動検出)",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="利用可能な音声デバイスを一覧表示して終了",
    )
    parser.add_argument(
        "--no-correct", action="store_true",
        help="LLMテキスト校正をスキップ",
    )
    parser.add_argument(
        "--no-clipboard", action="store_true",
        help="クリップボードへのコピーをしない",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="ファイルへの保存をしない",
    )
    parser.add_argument(
        "--output-dir", type=str, default="output",
        help="保存先ディレクトリ (default: output/)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_devices:
        list_audio_sources()
        return

    client = create_groq_client()

    # レコーダーを作成
    if args.mode == "mic":
        recorder = MicRecorder(device=args.device)
    elif args.mode == "system":
        recorder = SystemAudioRecorder(monitor_source=args.monitor)
    else:
        recorder = CombinedRecorder(mic_device=args.device, monitor_source=args.monitor)

    mode_label = {"mic": "マイク", "system": "システム音声", "both": "マイク+システム音声"}
    print(f"モード: {mode_label[args.mode]}")
    print("Enterキーで録音開始、もう一度Enterキーで録音停止")
    print("Ctrl+C で終了")
    print()

    try:
        while True:
            input(">> Enterキーを押して録音開始...")

            print("● 録音中... (Enterキーで停止)")
            recorder.start()
            input()

            print("■ 録音停止。処理中...")
            audio_data = recorder.stop()

            if len(audio_data) == 0:
                print("音声が録音されませんでした。もう一度お試しください。")
                print()
                continue

            # WAVバイト列に変換
            wav_bytes = numpy_to_wav_bytes(audio_data)
            duration_sec = len(audio_data) / SAMPLE_RATE
            size_mb = len(wav_bytes) / (1024 * 1024)
            print(f"  録音時間: {duration_sec:.1f}秒 ({size_mb:.1f}MB)")

            # 文字起こし
            print("文字起こし中 (Groq Whisper)...")
            raw_text = transcribe_audio(client, wav_bytes)

            if not raw_text.strip():
                print("音声が認識されませんでした。もう一度お試しください。")
                print()
                continue

            # テキスト校正
            if not args.no_correct:
                print("テキスト校正中 (Groq Llama 3.3 70B)...")
                corrected_text = correct_text(client, raw_text)
            else:
                corrected_text = raw_text

            # 結果出力
            display_results(
                raw_text,
                corrected_text,
                save=not args.no_save,
                clipboard=not args.no_clipboard,
                output_dir=args.output_dir,
            )
            print()

    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
