"""Generate synthetic video/NFO data for QA, never use personal media."""
import json
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as core
import desktop
from imageio_ffmpeg import get_ffmpeg_exe

root = Path(os.environ['YINGKU_DATA_DIR']).resolve()
root.mkdir(parents=True, exist_ok=True)
media = root / '测试影片（非个人资料）'
media.mkdir(exist_ok=True)
video = media / '旅途测试.2026.mp4'
subprocess.run([get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'testsrc2=size=640x360:rate=12', '-t', '12', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)], check=True)
frames = desktop.extract_keyframes(str(video), root / 'screenshots' / 'fixture')
video.with_suffix('.nfo').write_text('<movie><title>旅途测试 · 本地资料验证</title><year>2026</year><plot>此影片由测试图案生成，仅用于验证 Mac 版的扫描、封面、中文显示、评分与关键截图。正式影库不包含这些测试数据。</plot><genre>测试</genre><actor><name>示例演员</name><role>演示</role></actor></movie>')
core.init_db()
with core.connect() as conn:
    conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', [('min_duration_minutes','0'),('hide_files_after_import','false'),('auto_match_after_scan','false')])
core.scan_roots([str(media)])
with core.connect() as conn:
    row = conn.execute('SELECT id FROM movies LIMIT 1').fetchone()
    conn.execute("UPDATE movies SET local_poster=?,screenshots_json=?,screenshots_status='ready',personal_rating=8,favorite=1,watch_status='watched' WHERE id=?", (frames[0],json.dumps(frames),row['id']))
    timestamp=core.now_iso()
    for index,title in enumerate(['海边的清晨（示例）','城市夜行（示例）','山间来信（示例）','周末放映室（示例）','雨后（示例）']):
        conn.execute('INSERT INTO movies(path,filename,title,year,local_poster,cast_json,match_status,screenshots_json,screenshots_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (str(media / f'example{index}.mp4'),f'example{index}.mp4',title,2026,frames[index%len(frames)],json.dumps([{'name':'示例演员','role':'演示','avatar':frames[0]}],ensure_ascii=False),'manual',json.dumps(frames),'ready',timestamp,timestamp))
print(json.dumps({'duration': core.probe_video_duration(video), 'frames':len(frames), 'database':str(core.DB_PATH)},ensure_ascii=False))
