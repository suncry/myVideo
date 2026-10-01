"""Windowed entry point; unexpected startup failures leave a readable log."""
from pathlib import Path
import os,sys,traceback


def main():
    try:
        import desktop
        desktop.main()
    except SystemExit:raise
    except Exception:
        data=Path(os.environ.get('YINGKU_DATA_DIR') or Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')/'YingKu')
        data.mkdir(parents=True,exist_ok=True);log=data/'startup-error.log'
        log.write_text(traceback.format_exc(),encoding='utf-8')
        if sys.platform=='win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,f'影库未能启动，请将此日志提供给开发人员：\n{log}','影库',0x10)
        else:raise


if __name__=='__main__':main()
