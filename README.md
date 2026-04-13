# Groq Voice Input

Groq APIを使った音声入力スクリプト。Superwhisperの代替として、**音声入力 → Whisper文字起こし → Llama 3.3 70B自動校正**のワークフローを再現します。

macOS / Linux 対応。Zoom会議のシステム音声キャプチャにも対応。

## かんたんセットアップ（macOS）

```bash
git clone https://github.com/ikapulpo/whisper.git
cd whisper
bash install.sh
```

これだけで Homebrew・portaudio・ffmpeg・BlackHole・Pythonパッケージのインストールと APIキーの設定まで全自動で行います。

インストール後は **`start.command` をFinderでダブルクリック** するだけで起動できます。

## 手動セットアップ

<details>
<summary>クリックで展開</summary>

### 1. リポジトリを取得

```bash
git clone https://github.com/ikapulpo/whisper.git
cd whisper
```

### 2. システムパッケージをインストール

**macOS:**

```bash
brew install portaudio ffmpeg blackhole-2ch
```

**Linux (Ubuntu/Debian):**

```bash
sudo apt install libportaudio2 ffmpeg pulseaudio-utils xclip
```

### 3. Pythonパッケージをインストール

```bash
pip install -r requirements.txt
```

### 4. Groq APIキーを設定

[Groq Console](https://console.groq.com/) でAPIキーを取得し、環境変数に設定:

```bash
export GROQ_API_KEY=your_api_key_here
```

永続化するなら:

```bash
# macOS (zsh)
echo 'export GROQ_API_KEY=your_api_key_here' >> ~/.zshrc
source ~/.zshrc

# Linux (bash)
echo 'export GROQ_API_KEY=your_api_key_here' >> ~/.bashrc
source ~/.bashrc
```

</details>

## 使い方

### GUI版（推奨）

```bash
python groq_voice_gui.py
```

ボタン操作で録音開始/停止。モード選択・議事録生成・クリップボードコピー・保存フォルダを開くなど全てGUIから操作できます。

### CLI版（マイク入力）

```bash
python groq_voice.py
```

Enterキーで録音開始、もう一度Enterで停止。Ctrl+Cで終了。

### Zoomシステム音声をキャプチャ

```bash
python groq_voice.py --mode system
```

- **macOS**: BlackHole を自動検出して録音（初回は `--setup` でセットアップ）
- **Linux**: PulseAudio モニターソースを自動検出して録音

### マイク＋システム音声を同時録音

```bash
python groq_voice.py --mode both
```

自分の声とZoomの相手の声を両方キャプチャしてミックスします。

### 議事録を自動生成

```bash
python groq_voice.py --mode system --minutes
python groq_voice.py --mode both --minutes
```

録音 → 文字起こし → テキスト校正 → 議事録生成 を一気に実行します。
議事録は `output/minutes_YYYYMMDD_HHMMSS.txt` に保存され、クリップボードにもコピーされます。

## macOS: Zoomシステム音声の設定

Zoom等のシステム音声をキャプチャするには **BlackHole** が必要です。

```bash
# BlackHole をインストール
brew install blackhole-2ch

# セットアップガイドを表示
python groq_voice.py --setup
```

`--setup` を実行すると、Audio MIDI設定での複数出力装置の作成手順が表示されます。

設定完了後:

```bash
# 動作確認（BlackHole が検出されるか）
python groq_voice.py --list-devices

# Zoomシステム音声を録音
python groq_voice.py --mode system

# マイク + Zoom音声を同時録音
python groq_voice.py --mode both
```

## オプション一覧

```
--mode {mic,system,both}  音声ソース (default: mic)
--device INDEX             デバイスのインデックス番号 (--list-devices で確認)
--monitor SOURCE           [Linux] PulseAudioモニターソース名 (自動検出可)
--list-devices             利用可能なデバイスを一覧表示
--setup                    [macOS] BlackHoleセットアップガイドを表示
--minutes                  議事録を自動生成
--no-correct               LLM校正をスキップ（生テキストのみ）
--no-clipboard             クリップボードへのコピーをしない
--no-save                  ファイルへの保存をしない
--output-dir DIR           保存先ディレクトリ (default: output/)
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

### macOS: 「BlackHole が見つかりません」

```bash
brew install blackhole-2ch
python groq_voice.py --setup  # セットアップ手順を確認
```

### macOS: sounddevice がエラーを出す

```bash
brew install portaudio
```

### Linux: 「PulseAudioモニターソースが見つかりません」

```bash
pulseaudio --check    # 動作確認
pulseaudio --start    # 起動
pactl list short sources  # ソース一覧

# 手動でモニターソースを指定
python groq_voice.py --mode system --monitor alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
```

### 「GROQ_API_KEY 環境変数が設定されていません」

```bash
export GROQ_API_KEY=your_api_key_here
```
