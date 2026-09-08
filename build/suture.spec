# PyInstaller 打包配置。
# 六个平台各自在对应系统的机器上构建——PyInstaller 不能跨系统交叉编译，
# 这是工具本身的限制，不是流程繁琐。macOS 上用 universal2 把两种架构合成一个文件。
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
target_arch = os.environ.get("SUTURE_TARGET_ARCH") or None    # macOS 上传 universal2

sys.path.insert(0, ROOT)
try:
    from suture import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "0.0.0"

ICON_DIR = os.path.join(ROOT, "build", "icons")
WIN_ICON = os.path.join(ICON_DIR, "suture.ico")
MAC_ICON = os.path.join(ICON_DIR, "suture.icns")
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")

# pywebview 到底用哪个后端是运行时按平台/已装库探测出来的，这类动态导入
# PyInstaller 的静态扫描天然看不到——用 collect_submodules 把整个包一起收进来，
# 而不是猜某一个具体子模块的名字（猜错了会直接导致构建失败，比不收更糟）。
# 装没装 pywebview 都要能构建成功：没装时 collect_submodules 会抛异常，
# 接住就是了，跟"没装也能跑、退回浏览器"的既有设计保持一致。
hidden = []
try:
    from PyInstaller.utils.hooks import collect_submodules
    hidden += collect_submodules("webview")
except Exception:
    pass
if sys.platform == "win32":
    # winforms/edgechromium 后端靠 pythonnet 在运行时加载 .NET 程序集，
    # 这条路径也不是静态导入能看见的。
    for extra_mod in ("clr", "clr_loader"):
        try:
            __import__(extra_mod)
            hidden.append(extra_mod)
        except ImportError:
            pass

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "suture", "ui", "index.html"), os.path.join("suture", "ui")),
        (os.path.join(ROOT, "gateway_profile.json"), "."),
    ],
    hiddenimports=hidden,
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
    icon=WIN_ICON if os.path.exists(WIN_ICON) else None,
    version=VERSION_FILE if sys.platform == "win32" and os.path.exists(VERSION_FILE) else None,
)

# macOS 上把可执行文件包成真正的 .app：Finder 双击才会当成一个应用打开，
# 不是弹一个终端窗口。非 macOS 平台不需要这一步，EXE() 产物已经是最终形态。
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Suture.app",
        icon=MAC_ICON if os.path.exists(MAC_ICON) else None,
        bundle_identifier="com.yottastudios.suture",
        info_plist={
            "CFBundleName": "Suture",
            "CFBundleDisplayName": "Suture",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "YottaStudios",
        },
    )
