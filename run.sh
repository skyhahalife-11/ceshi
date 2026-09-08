#!/usr/bin/env bash
# 双击运行（Linux/macOS）。跟 run.bat 是同一件事，只是这个平台没有 .exe 那种
# 双击就能跑的形态，图形界面里点了这个文件通常会被当成脚本直接执行。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python not found. Install Python 3.11+ first:"
    echo "  https://www.python.org/downloads/"
    echo
    read -rp "Press Enter to exit..." _
    exit 1
fi

python3 main.py
read -rp "Press Enter to exit..." _
