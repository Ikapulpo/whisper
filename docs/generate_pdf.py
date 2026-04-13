#!/usr/bin/env python3
"""manual.md を PDF に変換するスクリプト。"""

import markdown
from weasyprint import HTML, CSS
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

md_path = SCRIPT_DIR / "manual.md"
pdf_path = SCRIPT_DIR / "manual.pdf"

md_text = md_path.read_text(encoding="utf-8")

# Markdown → HTML
html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])

# HTML テンプレート（日本語フォント対応）
# base_url を設定して画像の相対パスを解決
html_full = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"></head>
<body>{html_body}</body>
</html>"""

# CSS スタイル
css = CSS(string="""
@page {{
    size: A4;
    margin: 20mm 18mm 20mm 18mm;
}}
body {{
    font-family: "Noto Serif CJK JP", "Hiragino Mincho ProN", "Yu Mincho",
                 "MS Mincho", serif;
    font-size: 10.5pt;
    line-height: 1.8;
    color: #222;
}}
h1, h2, h3 {{
    font-family: "Noto Sans CJK JP", "Hiragino Kaku Gothic ProN", "Yu Gothic",
                 "MS Gothic", sans-serif;
}}
h1 {{
    font-size: 20pt;
    color: #1a5276;
    border-bottom: 3px solid #1a5276;
    padding-bottom: 8px;
    margin-top: 0;
}}
h2 {{
    font-size: 14pt;
    color: #2c3e50;
    border-bottom: 1px solid #bdc3c7;
    padding-bottom: 5px;
    margin-top: 25px;
}}
h3 {{
    font-size: 11.5pt;
    color: #34495e;
    margin-top: 18px;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}}
th, td {{
    border: 1px solid #bdc3c7;
    padding: 8px 12px;
    text-align: left;
}}
th {{
    background-color: #ecf0f1;
    font-weight: bold;
}}
th, td {{
    font-family: "Noto Sans CJK JP", "Hiragino Kaku Gothic ProN", sans-serif;
    font-size: 9.5pt;
}}
code {{
    background-color: #f4f4f4;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 9.5pt;
    font-family: "Noto Sans Mono CJK JP", "Menlo", "Consolas", monospace;
}}
pre {{
    background-color: #f5f5f5;
    color: #333;
    padding: 14px;
    border: 1px solid #ddd;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 9pt;
    line-height: 1.5;
}}
pre code {{
    background-color: transparent;
    color: inherit;
    padding: 0;
}}
hr {{
    border: none;
    border-top: 1px solid #ddd;
    margin: 20px 0;
}}
strong {{
    color: #c0392b;
}}
img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 15px auto;
}}
""")

# base_url で画像の相対パスを解決（flowchart.png など）
HTML(string=html_full, base_url=str(SCRIPT_DIR)).write_pdf(str(pdf_path), stylesheets=[css])
print(f"PDF を生成しました: {pdf_path}")
