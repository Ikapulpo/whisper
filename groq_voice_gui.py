#!/usr/bin/env python3
"""
Groq Voice Input — GUI版

tkinterベースのGUIフロントエンド。
groq_voice.py のコアロジックを再利用し、ボタン操作で録音・文字起こし・校正を行う。
"""

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

from groq_voice import (
    IS_MACOS,
    SAMPLE_RATE,
    CombinedRecorder,
    LinuxSystemAudioRecorder,
    MacSystemAudioRecorder,
    MicRecorder,
    copy_to_clipboard,
    correct_text,
    create_groq_client,
    generate_minutes,
    numpy_to_wav_bytes,
    save_to_file,
    transcribe_audio,
)


class GroqVoiceApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Groq Voice Input")
        self.geometry("700x600")
        self.minsize(500, 450)

        # 状態
        self.recording = False
        self.processing = False
        self.recorder = None
        self.record_start_time = 0
        self.timer_id = None

        # 結果
        self.raw_text = ""
        self.corrected_text = ""
        self.minutes_text = ""

        # Groqクライアント
        self.client = None

        self._build_ui()
        self._init_client()

    def _build_ui(self):
        # ── 上部: モード選択 ──
        top_frame = ttk.Frame(self, padding=10)
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="モード:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="mic")
        mode_combo = ttk.Combobox(
            top_frame,
            textvariable=self.mode_var,
            values=["mic", "system", "both"],
            state="readonly",
            width=10,
        )
        mode_combo.pack(side=tk.LEFT, padx=(5, 20))

        # モードラベル
        self.mode_labels = {"mic": "マイク", "system": "システム音声", "both": "マイク+システム"}
        self.mode_label = ttk.Label(top_frame, text="マイク")
        self.mode_label.pack(side=tk.LEFT)
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        # 議事録チェックボックス
        self.minutes_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top_frame, text="議事録生成", variable=self.minutes_var).pack(
            side=tk.RIGHT
        )

        # ── 中央: 録音ボタン ──
        btn_frame = ttk.Frame(self, padding=10)
        btn_frame.pack(fill=tk.X)

        self.record_btn = tk.Button(
            btn_frame,
            text="録音開始",
            font=("Helvetica", 18, "bold"),
            bg="#4CAF50",
            fg="white",
            activebackground="#45a049",
            activeforeground="white",
            height=2,
            command=self.toggle_recording,
        )
        self.record_btn.pack(fill=tk.X, padx=20)

        # ── ステータスバー ──
        status_frame = ttk.Frame(self, padding=(10, 0))
        status_frame.pack(fill=tk.X)

        self.status_label = ttk.Label(status_frame, text="待機中")
        self.status_label.pack(side=tk.LEFT)

        self.timer_label = ttk.Label(status_frame, text="00:00", font=("Courier", 12))
        self.timer_label.pack(side=tk.RIGHT)

        # ── タブ: 結果表示 ──
        self.notebook = ttk.Notebook(self, padding=5)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 生テキストタブ
        raw_frame = ttk.Frame(self.notebook)
        self.raw_text_widget = tk.Text(raw_frame, wrap=tk.WORD, font=("Helvetica", 13))
        raw_scroll = ttk.Scrollbar(raw_frame, command=self.raw_text_widget.yview)
        self.raw_text_widget.configure(yscrollcommand=raw_scroll.set)
        raw_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.raw_text_widget.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(raw_frame, text="生テキスト")

        # 校正済みタブ
        corrected_frame = ttk.Frame(self.notebook)
        self.corrected_text_widget = tk.Text(
            corrected_frame, wrap=tk.WORD, font=("Helvetica", 13)
        )
        corrected_scroll = ttk.Scrollbar(
            corrected_frame, command=self.corrected_text_widget.yview
        )
        self.corrected_text_widget.configure(yscrollcommand=corrected_scroll.set)
        corrected_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.corrected_text_widget.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(corrected_frame, text="校正済み")

        # 議事録タブ
        minutes_frame = ttk.Frame(self.notebook)
        self.minutes_text_widget = tk.Text(
            minutes_frame, wrap=tk.WORD, font=("Helvetica", 13)
        )
        minutes_scroll = ttk.Scrollbar(
            minutes_frame, command=self.minutes_text_widget.yview
        )
        self.minutes_text_widget.configure(yscrollcommand=minutes_scroll.set)
        minutes_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.minutes_text_widget.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(minutes_frame, text="議事録")

        # ── 下部: アクションボタン ──
        action_frame = ttk.Frame(self, padding=10)
        action_frame.pack(fill=tk.X)

        ttk.Button(action_frame, text="クリップボードにコピー", command=self.copy_result).pack(
            side=tk.LEFT, padx=(0, 5)
        )
        ttk.Button(action_frame, text="ファイル保存", command=self.save_result).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(action_frame, text="保存フォルダを開く", command=self.open_output_folder).pack(
            side=tk.LEFT, padx=5
        )

    def _init_client(self):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            self.set_status("エラー: GROQ_API_KEY が未設定です")
            self.record_btn.configure(state=tk.DISABLED)
            return
        self.client = create_groq_client()
        self.set_status("準備完了")

    def _on_mode_change(self, event=None):
        mode = self.mode_var.get()
        self.mode_label.configure(text=self.mode_labels.get(mode, mode))

    def set_status(self, text):
        self.status_label.configure(text=text)

    def toggle_recording(self):
        if self.processing:
            return
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        mode = self.mode_var.get()

        try:
            if mode == "mic":
                self.recorder = MicRecorder()
            elif mode == "system":
                if IS_MACOS:
                    self.recorder = MacSystemAudioRecorder()
                else:
                    self.recorder = LinuxSystemAudioRecorder()
            else:
                self.recorder = CombinedRecorder()
        except (RuntimeError, SystemExit) as e:
            self.set_status(f"エラー: {e}")
            return

        self.recorder.start()
        self.recording = True
        self.record_start_time = time.time()

        self.record_btn.configure(text="録音停止", bg="#f44336", activebackground="#d32f2f")
        self.set_status("録音中...")
        self._update_timer()

    def stop_recording(self):
        self.recording = False
        if self.timer_id:
            self.after_cancel(self.timer_id)
            self.timer_id = None

        self.set_status("録音停止。処理中...")
        self.record_btn.configure(
            text="処理中...", bg="#FF9800", activebackground="#F57C00", state=tk.DISABLED
        )

        audio_data = self.recorder.stop()

        if len(audio_data) == 0:
            self.set_status("音声が録音されませんでした")
            self._reset_button()
            return

        # バックグラウンドで処理
        self.processing = True
        thread = threading.Thread(target=self._process_audio, args=(audio_data,), daemon=True)
        thread.start()

    def _process_audio(self, audio_data):
        try:
            wav_bytes = numpy_to_wav_bytes(audio_data)
            duration_sec = len(audio_data) / SAMPLE_RATE

            # 文字起こし
            self.after(0, lambda: self.set_status(
                f"文字起こし中... ({duration_sec:.1f}秒)"
            ))
            self.raw_text = transcribe_audio(self.client, wav_bytes)

            if not self.raw_text.strip():
                self.after(0, lambda: self.set_status("音声が認識されませんでした"))
                self.after(0, self._reset_button)
                self.processing = False
                return

            self.after(0, lambda: self._set_text(self.raw_text_widget, self.raw_text))

            # 校正
            self.after(0, lambda: self.set_status("テキスト校正中..."))
            self.corrected_text = correct_text(self.client, self.raw_text)
            self.after(0, lambda: self._set_text(self.corrected_text_widget, self.corrected_text))

            # 議事録
            self.minutes_text = ""
            if self.minutes_var.get():
                self.after(0, lambda: self.set_status("議事録生成中..."))
                self.minutes_text = generate_minutes(self.client, self.corrected_text)
                self.after(0, lambda: self._set_text(self.minutes_text_widget, self.minutes_text))
                self.after(0, lambda: self.notebook.select(2))  # 議事録タブに切り替え
            else:
                self.after(0, lambda: self.notebook.select(1))  # 校正済みタブに切り替え

            # クリップボードにコピー
            clip_text = self.minutes_text if self.minutes_text else self.corrected_text
            copy_to_clipboard(clip_text)

            self.after(0, lambda: self.set_status(
                f"完了 ({duration_sec:.1f}秒の録音を処理) — クリップボードにコピー済み"
            ))

        except Exception as e:
            self.after(0, lambda: self.set_status(f"エラー: {e}"))

        finally:
            self.processing = False
            self.after(0, self._reset_button)

    def _set_text(self, widget, text):
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)

    def _reset_button(self):
        self.record_btn.configure(
            text="録音開始",
            bg="#4CAF50",
            activebackground="#45a049",
            state=tk.NORMAL,
        )

    def _update_timer(self):
        if not self.recording:
            return
        elapsed = int(time.time() - self.record_start_time)
        minutes = elapsed // 60
        seconds = elapsed % 60
        self.timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
        self.timer_id = self.after(1000, self._update_timer)

    def copy_result(self):
        tab_index = self.notebook.index(self.notebook.select())
        if tab_index == 0:
            text = self.raw_text
        elif tab_index == 1:
            text = self.corrected_text
        else:
            text = self.minutes_text

        if text and copy_to_clipboard(text):
            self.set_status("クリップボードにコピーしました")
        else:
            self.set_status("コピーするテキストがありません")

    def save_result(self):
        if self.corrected_text:
            path = save_to_file(self.corrected_text, prefix="transcription")
            self.set_status(f"保存: {path}")
        if self.minutes_text:
            path = save_to_file(self.minutes_text, prefix="minutes")
            self.set_status(f"議事録保存: {path}")
        if not self.corrected_text and not self.minutes_text:
            self.set_status("保存するテキストがありません")

    def open_output_folder(self):
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        abs_path = str(output_dir.resolve())

        if IS_MACOS:
            subprocess.Popen(["open", abs_path])
        else:
            subprocess.Popen(["xdg-open", abs_path])


def main():
    app = GroqVoiceApp()
    app.mainloop()


if __name__ == "__main__":
    main()
