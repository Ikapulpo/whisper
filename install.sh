#!/bin/bash
#
# Groq Voice Input — macOS インストーラー
#
# 使い方:
#   cd whisper
#   bash install.sh
#

set -e

echo "============================================"
echo "  Groq Voice Input インストーラー (macOS)"
echo "============================================"
echo ""

# ── 1. Homebrew チェック ──
if ! command -v brew &> /dev/null; then
    echo "[1/4] Homebrew が見つかりません。インストールします..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon の場合 PATH に追加
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
else
    echo "[1/4] Homebrew ... OK"
fi

# ── 2. システムパッケージ ──
echo "[2/4] システムパッケージをインストール中..."

install_if_missing() {
    if ! brew list "$1" &> /dev/null; then
        echo "  -> $1 をインストール中..."
        brew install "$1"
    else
        echo "  -> $1 ... OK"
    fi
}

install_if_missing portaudio
install_if_missing ffmpeg
install_if_missing blackhole-2ch

# ── 3. Python パッケージ ──
echo "[3/4] Python パッケージをインストール中..."

if ! command -v python3 &> /dev/null; then
    echo "  -> Python3 が見つかりません。インストールします..."
    install_if_missing python@3
fi

pip3 install -r requirements.txt --quiet

# ── 4. GROQ_API_KEY 設定 ──
echo "[4/4] APIキーの設定..."

SHELL_RC="$HOME/.zshrc"
if [ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if grep -q "GROQ_API_KEY" "$SHELL_RC" 2>/dev/null; then
    echo "  -> GROQ_API_KEY は既に設定されています"
else
    echo ""
    echo "  Groq APIキーを入力してください"
    echo "  (https://console.groq.com/ で取得できます)"
    echo ""
    read -p "  GROQ_API_KEY: " api_key
    if [ -n "$api_key" ]; then
        echo "" >> "$SHELL_RC"
        echo "export GROQ_API_KEY=$api_key" >> "$SHELL_RC"
        export GROQ_API_KEY="$api_key"
        echo "  -> $SHELL_RC に保存しました"
    else
        echo "  -> スキップしました（後で手動で設定してください）"
        echo "     echo 'export GROQ_API_KEY=あなたのキー' >> $SHELL_RC"
    fi
fi

# ── 完了 ──
echo ""
echo "============================================"
echo "  インストール完了!"
echo "============================================"
echo ""
echo "  起動方法:"
echo "    cd $(pwd)"
echo "    python3 groq_voice_gui.py"
echo ""
echo "  ※ BlackHole の初回設定が必要です:"
echo "    python3 groq_voice.py --setup"
echo ""
echo "  今すぐ起動しますか? (y/n)"
read -p "  > " launch
if [ "$launch" = "y" ] || [ "$launch" = "Y" ]; then
    source "$SHELL_RC" 2>/dev/null || true
    python3 groq_voice_gui.py
fi
