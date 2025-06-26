import os
from ninja import NinjaAPI
from pydantic import BaseModel
import yt_dlp 
from django.conf import settings
from backend.settings import MEDIA_ROOT, MEDIA_URL
import moviepy as mp
import cv2
import numpy as np

import re

class VideoInput(BaseModel):
    url: str 

def sanitize_filename(title):
    return re.sub(r'[<>:"/\\|?*]', '', title)

def find_best_start(filepath):
    cap = cv2.VideoCapture(filepath)
    frames_diff= []

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        grey = cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(grey,cv2.medianBlur(grey,5))
        frames_diff.append(diff.sum())

    cap.release()   

    best_start = frames_diff.index(max(frames_diff)) // 30 # Approximate seconds 
    return best_start
    

    




api = NinjaAPI()

@api.post("/generate-short")
def generate_short(request, data: VideoInput):
    url = data.url
    try:
        output_template = os.path.join(settings.MEDIA_ROOT, '%(title)s.%(ext)s')
        ydl_opts = {'format': 'best[height<=720]', 'outtmpl': output_template, 'noplaylist': True }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            video_info = ydl.extract_info(url, download=True)

        # ✅ Sanitize the title before building the path
        sanitized_title = sanitize_filename(video_info['title'])
        filepath = os.path.join(settings.MEDIA_ROOT, f"{sanitized_title}.mp4")

        if not os.path.exists(filepath):
            return {"error": f"Video file not found at {filepath}"}

        clip = mp.VideoFileClip(filepath)
        best_start = find_best_start(filepath)

        if best_start > clip.duration - 30:
            best_start = max(0, clip.duration - 30)

        short_clip = clip.subclipped(best_start - 2, best_start + 30)

        short_filename = f"{sanitized_title}_short.mp4"
        short_path = os.path.join(settings.MEDIA_ROOT, short_filename)
        short_clip.write_videofile(short_path, codec="libx264")

        return {
            "short_url": request.build_absolute_uri(f"{settings.MEDIA_URL}{short_filename}"),
            "title": video_info['title'],
            "duration": int(short_clip.duration)
        }

    except Exception as e:
        return {"error": str(e)}




