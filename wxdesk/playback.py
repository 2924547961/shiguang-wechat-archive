"""Cache browser-compatible video copies; keep originals if conversion fails."""
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from .common import check_cancel


def compatible_video(source, cancel=None):
    import imageio_ffmpeg
    source=Path(source);stat=source.stat()
    digest=hashlib.sha256((str(source)+str(stat.st_mtime_ns)+str(stat.st_size)).encode()).hexdigest()[:32]
    folder=source.parent/'playable';folder.mkdir(exist_ok=True)
    output=folder/(digest+'.mp4');marker=folder/(digest+'.compatible')
    if output.is_file() and output.stat().st_size:return output
    if marker.exists():return source
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0);exe=imageio_ffmpeg.get_ffmpeg_exe()
    probe=subprocess.run([exe,'-hide_banner','-i',str(source)],capture_output=True,timeout=30,creationflags=flags)
    info=probe.stderr.decode('utf-8','replace')
    if 'Video: h264' in info and ('Audio:' not in info or 'Audio: aac' in info) and source.suffix.lower()=='.mp4':
        marker.touch();return source
    temp=folder/(digest+'.partial.mp4')
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen([exe,'-nostdin','-y','-v','error','-i',str(source),'-map','0:v:0','-map','0:a?',
                                  '-c:v','libx264','-preset','veryfast','-crf','23','-pix_fmt','yuv420p','-c:a','aac',
                                  '-movflags','+faststart',str(temp)],stdout=log,stderr=log,creationflags=flags)
        try:
            while True:
                check_cancel(cancel)
                try:code=process.wait(timeout=.25);break
                except subprocess.TimeoutExpired:pass
            if code==0 and temp.is_file() and temp.stat().st_size:
                os.replace(temp,output);return output
            return source
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=10)
            temp.unlink(missing_ok=True)
