# Build on the target Mac; preserve the original native Qt UI.
from PyInstaller.utils.hooks import collect_all

ff_datas, ff_binaries, ff_imports = collect_all('imageio_ffmpeg')
a = Analysis(
    ['desktop.py'], pathex=[],
    binaries=ff_binaries + [('native/yingku-auth', 'native'), ('native/yingku-video-defaults', 'native'), ('native/yingku-iina-session', 'native'), ('native/libyingku-auth.dylib', 'native')],
    datas=[('assets', 'assets')] + ff_datas,
    hiddenimports=['javdb.__main__'] + ff_imports,
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml', 'PySide6.QtQuick'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='影库',
          console=False, strip=False, upx=False, target_arch='arm64',
          argv_emulation=False, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name='影库', strip=False, upx=False)
app = BUNDLE(coll, name='影库.app', icon='assets/app-icon.icns',
             bundle_identifier='local.yingku.desktop',
             info_plist={
                 'CFBundleDisplayName': '影库',
                 'CFBundleShortVersionString': '2.8.1',
                 'CFBundleVersion': '2.8.1',
                 'NSHighResolutionCapable': True,
                 'LSMinimumSystemVersion': '13.0',
                 'NSPrincipalClass': 'NSApplication',
                 'NSDesktopFolderUsageDescription': '读取你选择的桌面影片文件夹，用于建立本地影库。',
                 'NSDocumentsFolderUsageDescription': '读取你选择的文稿影片文件夹，用于建立本地影库。',
                 'NSDownloadsFolderUsageDescription': '读取你选择的下载影片文件夹，用于建立本地影库。',
                 'NSRemovableVolumesUsageDescription': '读取你选择的外接硬盘影片，用于建立本地影库。',
                 'NSNetworkVolumesUsageDescription': '读取你选择的网络磁盘影片，用于建立本地影库。',
             })
