# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import VSVersionInfo,FixedFileInfo,StringFileInfo,StringTable,StringStruct,VarFileInfo,VarStruct
if sys.platform!='win32':raise RuntimeError('Windows packages must be built with Windows Python.')
datas=[('assets','assets')]
binaries=[];hiddenimports=['javdb.__main__']
media=collect_all('imageio_ffmpeg');datas+=media[0];binaries+=media[1];hiddenimports+=media[2]
version=tuple(map(int,open('VERSION',encoding='utf-8').read().strip().split('.')))+(0,)
metadata=VSVersionInfo(ffi=FixedFileInfo(filevers=version,prodvers=version,mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('080404B0',[
 StringStruct('CompanyName','YingKu'),StringStruct('FileDescription','影库 Windows 桌面客户端'),StringStruct('FileVersion','.'.join(map(str,version))),StringStruct('ProductName','影库'),StringStruct('ProductVersion','.'.join(map(str,version))),StringStruct('OriginalFilename','影库.exe')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])
a=Analysis(['windows_entry.py'],pathex=[],binaries=binaries,datas=datas,hiddenimports=hiddenimports,hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['sitecustomize','usercustomize'],noarchive=False,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='影库',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,disable_windowed_traceback=False,icon='assets/app-icon.ico',version=metadata,contents_directory='_internal')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='影库')
