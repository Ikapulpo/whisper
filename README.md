# Groq Voice Input

Groq APIを使った音声入力スクリプト。Superwhisperの代替として、**音声入力 → Whisper文字起こし → Llama 3.3 70B自動校正**のワークフローをLinux上で再現します。

Zoom会議のシステム音声キャプチャにも対応。

## 必要なもの

### Groq APIキー

[Groq Console](https://console.groq.com/) でAPIキーを取得してください。

### システムパッケージ

```bash
# Ubuntu/Debian
sudo apt install libportaudio2 ffmpeg pulseaudio-utils xclip

# Fedora
sudo dnf install portaudio ffmpeg pulseaudio-utils xclip
```

### Pythonパッケージ

```bash
pip install -r requirements.txt
```

## セットアップ

```bash
# 1. リポジトリをクローン
git clone https://github.com/ikapulpo/whisper.git
cd whisper

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# 3. APIキーを設定
cp .env.example .env
# .env を編集して GROQ_API_KEY を設定

# または環境変数として直接設定
export GROQ_API_KEY=your_api_key_here
```

## 使い方

### 基本（マイク入力）

```bash
python groq_voice.py
```

Enterキーで録音開始、もう一度Enterキーで録音停止。Ctrl+Cで終了。

### Zoom会議のシステム音声をキャプチャ

```bash
python groq_voice.py --mode system
```

PulseAudioのモニターソースを自動検出し、システム音声（Zoomの相手の声など）を録音します。

### マイク＋システム音声を同時録音

```bash
python groq_voice.py --mode both
```

自分の声とZoomの相手の声を両方キャプチャしてミックスします。

### オプション一覧

```
--mode {mic,system,both}  音声ソース (default: mic)
--device INDEX             マイクデバイスのインデックス番号
--monitor SOURCE           PulseAudioモニターソース名 (自動検出可)
--list-devices             利用可能なデバイスを一覧表示
--no-correct               LLM校正をスキップ（生テキストのみ）
--no-clipboard             クリップボードへのコピーをしない
--no-save                  ファイルへの保存をしない
--output-dir DIR           保存先ディレクトリ (default: output/)
```

### デバイス確認

```bash
python groq_voice.py --list-devices
```

マイクデバイスとPulseAudioソースの一覧を表示します。特定のマイクを使う場合：

```bash
python groq_voice.py --device 2
```

## 処理フロー

```
[Enter] → 録音開始
  ↓
[Enter] → 録音停止
  ↓
Groq Whisper API (whisper-large-v3-turbo) で文字起こし
  ↓
Groq Llama 3.3 70B (llama-3.3-70b-versatile) で自動校正
  ↓
ターミナル表示 + クリップボードコピー + ファイル保存
```

## トラブルシューティング

### 「PulseAudioモニターソースが見つかりません」

PulseAudioが動作していることを確認してください：

```bash
pulseaudio --check
# 動作していなければ起動
pulseaudio --start
```

モニターソースを手動で確認：

```bash
pactl list short sources
```

`.monitor` を含むソース名を `--monitor` オプションで指定：

```bash
python groq_voice.py --mode system --monitor alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
```

### 「GROQ_API_KEY 環境変数が設定されていません」

```bash
export GROQ_API_KEY=your_api_key_here
```

### sounddevice がエラーを出す

PortAudioがインストールされていることを確認：

```bash
sudo apt install libportaudio2
```
