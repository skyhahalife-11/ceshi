# PyInstaller 打包配置。
# 六个平台各自在对应系统的机器上构建——PyInstaller 不能跨系统交叉编译，
# 这是工具本身的限制，不是流程繁琐。macOS 上用 universal2 把两种架构合成一个文件。
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
target_arch = os.environ.get("SUTURE_TARGET_ARCH") or None    # macOS 上传 universal2

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "suture", "ui", "index.html"), os.path.join("suture", "ui")),
        (os.path.join(ROOT, "gateway_profile.json"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # 引擎只用标准库；pywebview 装了就一起打包，没装也能跑（退回浏览器）
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="suture",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # 图形界面模式不弹黑窗口
    disable_windowed_traceback=False,
    target_arch=target_arch,
    codesign_identity=os.environ.get("SUTURE_CODESIGN_IDENTITY") or None,
    entitlements_file=None,
)
