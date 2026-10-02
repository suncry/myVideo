"""Validate a built executable against a disposable ordinary library."""
import argparse,json,os,subprocess,tempfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];exe=Path(args.exe).resolve();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='yingku-package-smoke-') as tmp:
        folder=Path(tmp);env=os.environ.copy();env.update(YINGKU_DATA_DIR=str(folder/'library'),YINGKU_SETTINGS_PATH=str(folder/'ui.ini'),YINGKU_DISABLE_STARTUP_TASKS='1',QT_QPA_PLATFORM='offscreen:configfile=tests/offscreen-screen.json',PYTHONUTF8='1')
        result=subprocess.run([str(exe),'--public','--smoke-test',str(output)],env=env,cwd=root,capture_output=True,timeout=90)
        (output/'stdout.log').write_bytes(result.stdout);(output/'stderr.log').write_bytes(result.stderr)
        if result.returncode:raise RuntimeError('Packaged startup failed: '+str(result.returncode))
        report=json.loads((output/'运行验证.json').read_text(encoding='utf-8'))
        assert report['database_integrity']=='ok' and report['cert_bundle'] and report['catalog_import']
        assert report['qt'].startswith('6.') and 'ffmpeg' in report['ffmpeg']
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
