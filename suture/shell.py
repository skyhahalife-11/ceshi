"""打开界面窗口。

先尝试系统自带的 WebView，拿到一个真正的应用窗口；初始化不成功（主要是
Linux 上没装 WebKitGTK 的情况）就退回到打开系统默认浏览器。两条路径用的是
同一套界面，只是承载的容器不同，不会出现「打开就报错、什么都做不了」。
"""
from __future__ import annotations

import os
import sys
import time
import webbrowser
from typing import Optional

from . import engine as E
from . import server as S

WINDOW_TITLE = "Suture · AI Gate 配置体检"


def _try_webview(url: str) -> bool:
    """pywebview 只在打包时附带，引擎本身不依赖它。装不上或初始化失败都返回 False。"""
    try:
        import webview  # noqa: PLC0415 —— 故意延迟导入，没有它也要能跑
    except Exception:
        return False
    try:
        webview.create_window(WINDOW_TITLE, url, width=860, height=760)
        webview.start()
        return True
    except Exception:
        return False


def launch(profile_path: Optional[str] = None, project_dir: Optional[str] = None,
           port: int = 0, open_ui: bool = True) -> int:
    engine = E.Engine(profile_path=profile_path, project_dir=project_dir)
    httpd, state, url = S.serve_in_background(engine, port=port)

    if not open_ui:
        print(url)
        return 0

    if _try_webview(url):
        httpd.shutdown()
        return 0

    # 没有可用的系统 WebView（主要是 Linux 上没装 WebKitGTK）：退回默认浏览器。
    # SUTURE_NO_BROWSER 留给无界面环境和自动化：照常提供服务，只是不主动拉起浏览器。
    skip_browser = os.environ.get("SUTURE_NO_BROWSER") == "1"
    print(f"{'服务已启动' if skip_browser else '已在浏览器中打开'}：{url}", flush=True)
    print("这个地址只在本机有效，带一次性令牌，关掉这个程序就失效。", flush=True)
    print("按 Ctrl+C 退出。", flush=True)
    if not skip_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(launch())
