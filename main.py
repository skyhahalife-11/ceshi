#!/usr/bin/env python3
"""打包成单文件可执行程序的入口。默认打开图形界面，
加 --cli 走命令行；构建方式见 build/ 目录。"""
import sys

from suture.cli import run


def _has_console() -> bool:
    """打包成窗口程序（PyInstaller console=False）之后，Windows 上拿不到控制台：
    sys.stdout 是 None，print 出去的东西直接丢掉，input() 还会抛
    RuntimeError: lost sys.stdin。这种情况下 --cli 是没法用的，要在进去之前就发现。"""
    return sys.stdout is not None and sys.stdin is not None


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--cli" in argv:
        argv.remove("--cli")
        if not _has_console():
            # 没有控制台还硬走命令行，用户看到的是「点了没反应」或者一个崩溃弹窗。
            # 退回图形界面是唯一还能把结果呈现出来的方式。
            argv = argv + ["--gui"]
    elif "--gui" not in argv:
        argv = argv + ["--gui"]
    sys.exit(run(argv))
