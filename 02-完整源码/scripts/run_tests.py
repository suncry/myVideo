"""Run all regression tests without reading or changing the user's library."""
import os,sys,tempfile,unittest
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[1];os.chdir(root);sys.path.insert(0,str(root))
    with tempfile.TemporaryDirectory(prefix='yingku-tests-') as tmp:
        folder=Path(tmp)
        os.environ.update(YINGKU_DATA_DIR=str(folder/'data'),YINGKU_SETTINGS_PATH=str(folder/'ui.ini'),YINGKU_DISABLE_STARTUP_TASKS='1')
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen:configfile=tests/offscreen-screen.json')
        suite=unittest.defaultTestLoader.discover(str(root/'tests'))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
