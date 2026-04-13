#!/usr/bin/env python3
"""
Groq Voice Input — GUI版

tkinterベースのGUIフロントエンド。
groq_voice.py のコアロジックを再利用し、ボタン操作で録音・文字起こし・校正を行う。
リアルタイム波形モニターで音声入力の有無を視覚的に確認可能。
"""

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

import numpy as np

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

# 無音判定の閾値（int16の最大値32768に対する比率）
SILENCE_RMS_THRESHOLD = 100  # RMSがこれ以下なら無音とみなす


class ColorButton(tk.Canvas):
    """macOSでも背景色が正しく表示されるカスタムボタン。"""

    def __init__(self, parent, text="", bg_color="#4CAF50", fg_color="white",
                 font=("Helvetica", 18, "bold"), height=60, command=None, **kwargs):
        super().__init__(parent, height=height, highlightthickness=0, **kwargs)
        self._bg_color = bg_color
        self._fg_color = fg_color
        self._font = font
        self._text = text
        self._command = command
        self._enabled = True

        self.bind("<Configure>", self._draw)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width() or 200
        h = self.winfo_height() or 60
        r = 12

        self.create_round_rect(2, 2, w - 2, h - 2, r, fill=self._bg_color, outline="")
        self.create_text(
            w // 2, h // 2, text=self._text,
            fill=self._fg_color, font=self._font,
        )

    def create_round_rect(self, x1, y1, x2, y2, r, **kwargs):
        self.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style=tk.PIESLICE, **kwargs)
        self.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style=tk.PIESLICE, **kwargs)
        self.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style=tk.PIESLICE, **kwargs)
        self.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style=tk.PIESLICE, **kwargs)
        self.create_rectangle(x1 + r, y1, x2 - r, y2, **kwargs)
        self.create_rectangle(x1, y1 + r, x2, y2 - r, **kwargs)

    def _on_click(self, event=None):
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, event=None):
        if self._enabled:
            self.configure(cursor="hand2")

    def _on_leave(self, event=None):
        self.configure(cursor="")

    def set_state(self, text=None, bg_color=None, fg_color=None, enabled=True):
        if text is not None:
            self._text = text
        if bg_color is not None:
            self._bg_color = bg_color
        if fg_color is not None:
            self._fg_color = fg_color
        self._enabled = enabled
        self._draw()


class WaveformMonitor(tk.Canvas):
    """リアルタイム波形表示 + レベルメーター。"""

    def __init__(self, parent, height=80, **kwargs):
        super().__init__(parent, height=height, bg="#1a1a2e", highlightthickness=0, **kwargs)
        self._height = height
        self._warning_visible = False
        self.bind("<Configure>", self._on_resize)
        self._draw_idle()

    def _on_resize(self, event=None):
        pass

    def _draw_idle(self):
        """待機中の表示。"""
        self.delete("all")
        w = self.winfo_width() or 600
        h = self._height
        mid_y = h // 2

        # 中央線
        self.create_line(0, mid_y, w, mid_y, fill="#333355", width=1)
        self.create_text(
            w // 2, mid_y, text="録音待機中",
            fill="#555577", font=("Helvetica", 11),
        )
        self._warning_visible = False

    def draw_waveform(self, samples: np.ndarray, rms: float):
        """波形とレベルメーターを描画する。"""
        self.delete("all")
        w = self.winfo_width() or 600
        h = self._height
        mid_y = h // 2
        level_bar_width = 40
        wave_width = w - level_bar_width - 10

        # ── 波形描画 ──
        self.create_line(0, mid_y, wave_width, mid_y, fill="#333355", width=1)

        if len(samples) > 0:
            # サンプルをキャンバス幅にリサンプル
            step = max(1, len(samples) // wave_width)
            points = []
            for i in range(0, min(len(samples), wave_width * step), step):
                x = i // step
                val = float(samples[i]) / 32768.0
                y = mid_y - int(val * (mid_y - 4))
                points.append(x)
                points.append(y)

            if len(points) >= 4:
                # 信号レベルで色分け
                if rms < SILENCE_RMS_THRESHOLD:
                    color = "#555577"  # 無音 = 暗い
                elif rms < 1000:
                    color = "#4CAF50"  # 小さい = 緑
                elif rms < 5000:
                    color = "#8BC34A"  # 普通 = 明るい緑
                else:
                    color = "#FF9800"  # 大きい = オレンジ
                self.create_line(points, fill=color, width=1.5, smooth=True)

        # ── レベルメーター ──
        bar_x = wave_width + 8
        bar_h = h - 10
        bar_y = 5

        # 背景
        self.create_rectangle(bar_x, bar_y, bar_x + level_bar_width - 4, bar_y + bar_h,
                              fill="#0d0d1a", outline="#333355")

        # レベルバー
        level = min(1.0, rms / 10000.0)
        fill_h = int(bar_h * level)
        if fill_h > 0:
            if level < 0.3:
                bar_color = "#4CAF50"
            elif level < 0.7:
                bar_color = "#8BC34A"
            else:
                bar_color = "#FF9800"
            self.create_rectangle(
                bar_x + 2, bar_y + bar_h - fill_h,
                bar_x + level_bar_width - 6, bar_y + bar_h,
                fill=bar_color, outline="",
            )

        # ── 無音警告 ──
        if rms < SILENCE_RMS_THRESHOLD:
            self.create_text(
                wave_width // 2, mid_y,
                text="音声信号なし — 入力設定を確認してください",
                fill="#ff5555", font=("Helvetica", 11, "bold"),
            )
            self._warning_visible = True
        else:
            self._warning_visible = False

    def draw_processing(self):
        """処理中の表示。"""
        self.delete("all")
        w = self.winfo_width() or 600
        h = self._height
        mid_y = h // 2
        self.create_text(
            w // 2, mid_y, text="処理中...",
            fill="#FF9800", font=("Helvetica", 11),
        )


class GroqVoiceApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Groq Voice Input")
        self.geometry("700x700")
        self.minsize(500, 550)

        # 状態
        self.recording = False
        self.processing = False
        self.recorder = None
        self.record_start_time = 0
        self.timer_id = None
        self.waveform_id = None
        self.silence_duration = 0  # 無音が続いた秒数

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

        self.mode_labels = {"mic": "マイク", "system": "システム音声", "both": "マイク+システム"}
        self.mode_label = ttk.Label(top_frame, text="マイク")
        self.mode_label.pack(side=tk.LEFT)
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        self.minutes_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top_frame, text="議事録生成", variable=self.minutes_var).pack(
            side=tk.RIGHT
        )

        # ── 録音ボタン ──
        btn_frame = ttk.Frame(self, padding=(10, 5))
        btn_frame.pack(fill=tk.X)

        self.record_btn = ColorButton(
            btn_frame,
            text="録音開始",
            bg_color="#4CAF50",
            fg_color="white",
            height=70,
            command=self.toggle_recording,
        )
        self.record_btn.pack(fill=tk.X, padx=20)

        # ── 波形モニター ──
        wave_frame = ttk.Frame(self, padding=(10, 5))
        wave_frame.pack(fill=tk.X)

        self.waveform = WaveformMonitor(wave_frame, height=80)
        self.waveform.pack(fill=tk.X, padx=20)

        # ── ステータスバー ──
        status_frame = ttk.Frame(self, padding=(10, 2))
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
            self.record_btn.set_state(text="APIキー未設定", bg_color="#9E9E9E", enabled=False)
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
        self.silence_duration = 0

        self.record_btn.set_state(text="録音停止", bg_color="#f44336")
        self.set_status("録音中...")
        self._update_timer()
        self._update_waveform()

    def stop_recording(self):
        self.recording = False
        if self.timer_id:
            self.after_cancel(self.timer_id)
            self.timer_id = None
        if self.waveform_id:
            self.after_cancel(self.waveform_id)
            self.waveform_id = None

        self.waveform.draw_processing()
        self.set_status("録音停止。処理中...")
        self.record_btn.set_state(text="処理中...", bg_color="#FF9800", enabled=False)

        audio_data = self.recorder.stop()

        if len(audio_data) == 0:
            self.set_status("音声が録音されませんでした — 入力デバイスの設定を確認してください")
            self.waveform._draw_idle()
            self._reset_button()
            return

        # 全体の音量チェック
        rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
        if rms < SILENCE_RMS_THRESHOLD:
            self.set_status("無音でした — システム音声の出力先が「複数出力装置」に設定されているか確認してください")
            self.waveform._draw_idle()
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
                self.after(0, self.waveform._draw_idle)
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
                self.after(0, lambda: self.notebook.select(2))
            else:
                self.after(0, lambda: self.notebook.select(1))

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
            self.after(0, self.waveform._draw_idle)
            self.after(0, self._reset_button)

    def _update_waveform(self):
        """100msごとに波形を更新する。"""
        if not self.recording:
            return

        try:
            samples = self.recorder.get_recent_samples(1600)
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            self.waveform.draw_waveform(samples, rms)

            # 無音カウント（100ms単位で加算）
            if rms < SILENCE_RMS_THRESHOLD:
                self.silence_duration += 0.1
                if self.silence_duration >= 3.0:
                    self.set_status("録音中... [音声信号なし — 入力設定を確認してください]")
            else:
                self.silence_duration = 0
                elapsed = int(time.time() - self.record_start_time)
                self.set_status(f"録音中... {elapsed}秒")
        except Exception:
            pass

        self.waveform_id = self.after(100, self._update_waveform)

    def _set_text(self, widget, text):
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)

    def _reset_button(self):
        self.record_btn.set_state(text="録音開始", bg_color="#4CAF50", enabled=True)

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
