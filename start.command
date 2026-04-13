#!/bin/bash
#
# ダブルクリックで Groq Voice Input を起動する
# (Finderからダブルクリックで使えます)
#
cd "$(dirname "$0")"
source ~/.zshrc 2>/dev/null || source ~/.bashrc 2>/dev/null || true
python3 groq_voice_gui.py
