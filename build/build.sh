#!/usr/bin/env bash
# 在当前这台机器上打一个单文件可执行程序。
# 六个目标需要六台对应系统/架构的机器，见 .github/workflows/release.yml。
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install --quiet --upgrade pyinstaller
# pywebview 让程序以应用窗口打开；装不上也不影响，运行时会退回默认浏览器
python3 -m pip install --quiet pywebview || echo "pywebview 装不上，界面将以浏览器方式打开"

if [[ "$(uname -s)" == "Darwin" ]]; then
  export SUTURE_TARGET_ARCH="${SUTURE_TARGET_ARCH:-universal2}"
fi

python3 -m PyInstaller --clean --noconfirm build/suture.spec
echo "产物：dist/suture"
