#!/usr/bin/env python3
"""打包成单文件可执行程序的入口。默认打开图形界面，
加 --cli 走命令行；构建方式见 build/ 目录。"""
import sys

from suture.cli import run

if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--cli" in argv:
        argv.remove("--cli")
    elif "--gui" not in argv:
        argv = argv + ["--gui"]
    sys.exit(run(argv))
