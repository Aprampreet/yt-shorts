"""Exercise API -> Redis -> Celery -> FFmpeg -> private downloads using synthetic footage.
Run while API, Redis and worker are running: .venv/bin/python scripts/smoke.py
"""
import argparse
import io
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
import httpx

parser = argparse.ArgumentParser()
parser.add_argument('--api', default='http://127.0.0.1:8000/api/v1')
args = parser.parse_args()
with tempfile.TemporaryDirectory() as temporary:
    source = Path(temporary)/'SYNTHETIC-pipeline-test.mp4'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-y','-f','lavfi','-i',
        'testsrc2=size=320x180:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
        '-t','62','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
    with httpx.Client(base_url=args.api, timeout=120, headers={'Origin': args.api.rsplit('/api/v1',1)[0]}) as client:
        token = client.get('/session').raise_for_status().json()['csrf_token']
        before = time.monotonic()
        with source.open('rb') as media:
            response = client.post('/uploads',headers={'X-CSRFToken':token},
                data={'rights_declaration':'true','permission_reference':'Original FFmpeg-generated test pattern; no third-party footage.',
                      'options':'{"target_duration":20,"require_captions":false}'},files={'file':(source.name,media,'video/mp4')})
        assert response.status_code == 202,response.text
        job = response.json()
        print(f'Accepted job {job["id"]} in {time.monotonic()-before:.2f}s',flush=True)
        deadline = time.monotonic()+180
        last = ''
        while job['status'] not in ['needs_review','completed','failed','cancelled']:
            assert time.monotonic() < deadline,'Job timed out'
            time.sleep(1)
            job = client.get('/jobs/'+job['id']).raise_for_status().json()
            if job['stage'] != last:
                print(f'{job["progress"]}% {job["stage"]}',flush=True)
                last = job['stage']
        assert job['status'] in ['needs_review','completed'],job
        assert len(job['results']) == 3,job
        for result in job['results']:
            preview = client.get(result['video_url'].replace('/api/v1',''), headers={'Range':'bytes=0-1023'})
            assert preview.status_code == 206 and len(preview.content) == 1024
            response = client.get(result['download_url'].replace('/api/v1',''))
            assert response.status_code == 200 and len(response.content)>1000
            print(f'Short {result["short_number"]}: {result["start_time"]}–{result["end_time"]}s, {len(response.content)} bytes, QA {result["quality_score"]}',flush=True)
        batch = client.get('/jobs/'+job['id']+'/download').raise_for_status()
        with zipfile.ZipFile(io.BytesIO(batch.content)) as archive:
            assert len([name for name in archive.namelist() if name.endswith('.mp4')]) == 3
        print('PASS: real asynchronous job, three distinct MP4s, private downloads and ZIP.',flush=True)
