#!/usr/bin/env python3
"""
Groq Voice Input — 音声入力 → Whisper文字起こし → LLM自動校正

Superwhisperの代替として、Groq APIを直接呼び出すPythonスクリプト。
勝間和代氏のワークフローを再現: 音声入力 → Whisper → Llama 3.3 70B校正
Zoom会議のシステム音声キャプチャにも対応（macOS / Linux）。
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

# ── プラットフォーム検出 ──────────────────────────────────────────────────────

IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

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

MINUTES_SYSTEM_PROMPT = """\
あなたは会議の議事録を作成する専門家です。

音声認識から得られた会議のテキストを基に、構造化された議事録を作成してください。

以下のフォーマットで出力してください：

# 議事録

## 概要
（会議の目的・テーマを1〜2文で要約）

## 参加者
（会話の内容から推定できる参加者を列挙。不明な場合は「参加者不明」と記載）

## 議論内容
（主要な議題ごとにまとめる。箇条書きで整理）

## 決定事項
（会議で決まったことを箇条書きで列挙。なければ「特になし」）

## アクションアイテム
（誰が何をいつまでにやるかを箇条書きで列挙。なければ「特になし」）

## 備考
（その他の重要な情報があれば記載。なければ省略）

注意事項：
- 元の発言の意味を変えないこと
- 推測で情報を追加しないこと
- 簡潔かつ正確に記述すること
- 議事録のみを出力し、説明やコメントは不要"""


# ── 音声デバイス検出 ──────────────────────────────────────────────────────────

def list_audio_sources():
    """利用可能な音声デバイスを一覧表示する。"""
    print("=== 音声デバイス一覧 ===")
    print(sd.query_devices())
    print()

    if IS_MACOS:
        # BlackHole の検出状況を表示
        bh = find_blackhole_device()
        if bh is not None:
            info = sd.query_devices(bh)
            print(f"[OK] BlackHole 検出済み: device {bh} - {info['name']}")
        else:
            print("[--] BlackHole が見つかりません。システム音声キャプチャには BlackHole が必要です。")
            print("     インストール: brew install blackhole-2ch")
            print("     セットアップ: python groq_voice.py --setup")
    elif IS_LINUX:
        print("=== PulseAudio ソース一覧 ===")
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                print(result.stdout)
            else:
                print("pactl の実行に失敗しました。")
        except FileNotFoundError:
            print("pactl が見つかりません。pulseaudio-utils をインストールしてください。")
        except subprocess.TimeoutExpired:
            print("pactl がタイムアウトしました。")


def find_blackhole_device() -> int | None:
    """BlackHole デバイスのインデックスを検索する（macOS用）。"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if "BlackHole" in dev["name"] and dev["max_input_channels"] > 0:
            return i
    return None


def find_aggregate_device() -> int | None:
    """集約装置（マイク+BlackHole）のインデックスを検索する（macOS用）。"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        # 集約装置は複数入力チャンネルを持つことが多い
        name = dev["name"].lower()
        if dev["max_input_channels"] >= 2 and (
            "aggregate" in name or "集約" in name or "multi" in name
        ):
            return i
    return None


def find_monitor_source() -> str:
    """PulseAudioモニターソースを自動検出する（Linux用）。"""
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


def print_setup_guide():
    """macOSでのBlackHoleセットアップガイドを表示する。"""
    guide = """
============================================================
  BlackHole セットアップガイド（macOS）
============================================================

Zoom等のシステム音声をキャプチャするには BlackHole が必要です。

【ステップ1】 BlackHole をインストール
  brew install blackhole-2ch

【ステップ2】 複数出力装置を作成（スピーカー + BlackHole）
  1. 「Audio MIDI設定」アプリを開く
     (Spotlight で "Audio MIDI設定" と検索、または
      /Applications/Utilities/Audio MIDI Setup.app)
  2. 左下の「＋」ボタン → 「複数出力装置を作成」
  3. 以下にチェックを入れる:
     - Mac のスピーカー (Built-in Output / MacBook Proのスピーカー)
     - BlackHole 2ch
  4. スピーカーを「マスターデバイス」に設定

【ステップ3】 音声出力を切り替え
  システム設定 → サウンド → 出力 → 「複数出力装置」を選択
  (これでスピーカーから音が聞こえつつ、BlackHole にも音声が流れます)

【ステップ4】 動作確認
  python groq_voice.py --list-devices
  → BlackHole 2ch が表示されればOK

【使い方】
  # Zoomシステム音声のみ
  python groq_voice.py --mode system

  # マイク + Zoom音声（両方同時）
  python groq_voice.py --mode both

============================================================
  集約装置の作成（--mode both をより確実に動作させる場合）
============================================================

「--mode both」はデフォルトで内蔵マイクとBlackHoleの2ストリームを
同時録音しますが、集約装置を使うとより安定します。

  1. 「Audio MIDI設定」で「＋」→「集約装置を作成」
  2. 「内蔵マイク」と「BlackHole 2ch」にチェック
  3. 以下のように使用:
     python groq_voice.py --device <集約装置のインデックス>
============================================================
"""
    print(guide)


# ── レコーダー ────────────────────────────────────────────────────────────────

class MicRecorder:
    """マイクから録音する（sounddevice使用）。macOS / Linux 共通。"""

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

    def get_recent_samples(self, n_samples: int = 1600) -> np.ndarray:
        """直近の録音サンプルを取得する（波形表示用）。"""
        if not self.frames:
            return np.zeros(n_samples, dtype=np.int16)
        last = self.frames[-1].flatten()
        if len(last) >= n_samples:
            return last[-n_samples:]
        return np.pad(last, (n_samples - len(last), 0))

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
        self.recording = False
        if not self.frames:
            return np.array([], dtype=np.int16)
        return np.concatenate(self.frames, axis=0).flatten()


class MacSystemAudioRecorder(MicRecorder):
    """macOS: BlackHole経由でシステム音声を録音する。"""

    def __init__(self, device=None, sample_rate=SAMPLE_RATE):
        if device is None:
            device = find_blackhole_device()
            if device is None:
                print("エラー: BlackHole が見つかりません。", file=sys.stderr)
                print("セットアップ方法: python groq_voice.py --setup", file=sys.stderr)
                sys.exit(1)
            info = sd.query_devices(device)
            print(f"BlackHole 検出: device {device} - {info['name']}")
        super().__init__(device=device, sample_rate=sample_rate)


class LinuxSystemAudioRecorder:
    """Linux: PulseAudio parec経由でシステム音声を録音する。"""

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

    def get_recent_samples(self, n_samples: int = 1600) -> np.ndarray:
        """直近の録音サンプルを取得する（波形表示用）。"""
        if len(self._raw_data) < 2:
            return np.zeros(n_samples, dtype=np.int16)
        n_bytes = n_samples * 2  # int16 = 2 bytes
        raw = bytes(self._raw_data[-n_bytes:])
        samples = np.frombuffer(raw, dtype=np.int16)
        if len(samples) >= n_samples:
            return samples[-n_samples:]
        return np.pad(samples, (n_samples - len(samples), 0))

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

    def __init__(self, mic_device=None, system_device=None, monitor_source=None):
        self.mic = MicRecorder(device=mic_device)
        if IS_MACOS:
            self.system = MacSystemAudioRecorder(device=system_device)
        else:
            self.system = LinuxSystemAudioRecorder(monitor_source=monitor_source)

    def start(self):
        self.system.start()
        self.mic.start()

    def get_recent_samples(self, n_samples: int = 1600) -> np.ndarray:
        """直近の録音サンプルを取得する（波形表示用）。マイク側を返す。"""
        return self.mic.get_recent_samples(n_samples)

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


def generate_minutes(client: Groq, corrected_text: str) -> str:
    """校正済みテキストから議事録を生成する。"""
    if not corrected_text.strip():
        return ""

    completion = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": MINUTES_SYSTEM_PROMPT},
            {"role": "user", "content": corrected_text},
        ],
        temperature=0.3,
        max_tokens=4096,
    )

    return completion.choices[0].message.content


# ── 出力 ──────────────────────────────────────────────────────────────────────

def copy_to_clipboard(text: str) -> bool:
    """テキストをクリップボードにコピーする。macOS: pbcopy / Linux: xclip,xsel"""
    if IS_MACOS:
        cmds = [["pbcopy"]]
    else:
        cmds = [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]

    for cmd in cmds:
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return False


def save_to_file(text: str, output_dir: str = "output", prefix: str = "transcription") -> Path:
    """テキストをタイムスタンプ付きファイルに保存する。"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = Path(output_dir) / f"{prefix}_{timestamp}.txt"
    filepath.write_text(text, encoding="utf-8")
    return filepath


def display_results(
    raw_text: str,
    corrected_text: str,
    minutes_text: str = None,
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

    if minutes_text:
        print()
        print("=" * 60)
        print("[議事録]")
        print("-" * 60)
        print(minutes_text)
        print("=" * 60)

    # クリップボードには議事録があればそれを、なければ校正テキストをコピー
    clip_text = minutes_text if minutes_text else corrected_text
    if clipboard:
        if copy_to_clipboard(clip_text):
            print("-> クリップボードにコピーしました")
        else:
            if IS_MACOS:
                print("-> クリップボードへのコピーに失敗しました")
            else:
                print("-> クリップボードへのコピーに失敗（xclip/xsel をインストールしてください）")

    if save:
        filepath = save_to_file(corrected_text, output_dir, prefix="transcription")
        print(f"-> ファイルに保存しました: {filepath}")
        if minutes_text:
            minutes_path = save_to_file(minutes_text, output_dir, prefix="minutes")
            print(f"-> 議事録を保存しました: {minutes_path}")


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
        help="[Linux] PulseAudioモニターソース名 (未指定なら自動検出)",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="利用可能な音声デバイスを一覧表示して終了",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="[macOS] BlackHoleセットアップガイドを表示して終了",
    )
    parser.add_argument(
        "--minutes", action="store_true",
        help="議事録を自動生成する（校正後テキストから）",
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


def create_recorder(args):
    """プラットフォームとモードに応じてレコーダーを作成する。"""
    if args.mode == "mic":
        return MicRecorder(device=args.device)

    if args.mode == "system":
        if IS_MACOS:
            return MacSystemAudioRecorder(device=args.device)
        else:
            return LinuxSystemAudioRecorder(monitor_source=args.monitor)

    # both
    if IS_MACOS:
        return CombinedRecorder(mic_device=args.device, system_device=None)
    else:
        return CombinedRecorder(mic_device=args.device, monitor_source=args.monitor)


def main():
    args = parse_args()

    if args.setup:
        if IS_MACOS:
            print_setup_guide()
        else:
            print("--setup は macOS 専用です。")
            print("Linux では pulseaudio-utils をインストールしてください:")
            print("  sudo apt install pulseaudio-utils")
        return

    if args.list_devices:
        list_audio_sources()
        return

    client = create_groq_client()
    recorder = create_recorder(args)

    mode_label = {"mic": "マイク", "system": "システム音声", "both": "マイク+システム音声"}
    platform_label = "macOS" if IS_MACOS else "Linux"
    print(f"プラットフォーム: {platform_label}")
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

            # 議事録生成
            minutes_text = None
            if args.minutes:
                print("議事録生成中 (Groq Llama 3.3 70B)...")
                minutes_text = generate_minutes(client, corrected_text)

            # 結果出力
            display_results(
                raw_text,
                corrected_text,
                minutes_text=minutes_text,
                save=not args.no_save,
                clipboard=not args.no_clipboard,
                output_dir=args.output_dir,
            )
            print()

    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
