#!/usr/bin/env bash
# 只检查、不改动本机任何东西。跟 check-only.bat 是同一件事。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python not found. Install Python 3.11+ first:"
    echo "  https://www.python.org/downloads/"
    echo
    read -rp "Press Enter to exit..." _
    exit 1
fi

echo "Read-only check. Nothing on this machine will be modified."
echo
python3 main.py --cli --no-fix
echo
read -rp "Press Enter to exit..." _
