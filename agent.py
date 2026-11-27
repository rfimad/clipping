import os
import glob
import json
import time
import asyncio
import subprocess
import requests
import gdown
import edge_tts
from google import genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

VIDEO_URL = os.environ.get("VIDEO_URL", "").strip()
CAMPAIGN_NOTES = os.environ.get("CAMPAIGN_NOTES", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()
TIKTOK_ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()

USED_VIDEOS_FILE = "used_videos.txt"
VOICE_NAME = "en-US-BrianNeural"
DRIVE_DIR = "drive_footage"
VIDEO_EXTS = ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.m4v")


def get_youtube_upload_client():
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def load_used_items() -> set:
    if not os.path.exists(USED_VIDEOS_FILE):
        return set()
    with open(USED_VIDEOS_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_used_item(item_key: str) -> None:
    with open(USED_VIDEOS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_key}\n")


def download_from_google_drive(drive_url: str, used_items: set) -> tuple[str, str]:
    if not drive_url:
        raise ValueError("VIDEO_URL is empty. Paste a Google Drive file or folder link.")

    os.makedirs(DRIVE_DIR, exist_ok=True)

    if "/folders/" in drive_url:
        gdown.download_folder(
            url=drive_url,
            output=DRIVE_DIR,
            quiet=False,
            use_cookies=False,
            remaining_ok=True,
        )
    else:
        out_path = os.path.join(DRIVE_DIR, "input_video.mp4")
        gdown.download(url=drive_url, output=out_path, quiet=False, fuzzy=True)

    files = []
    for ext in VIDEO_EXTS:
        files.extend(glob.glob(os.path.join(DRIVE_DIR, "**", ext), recursive=True))
    files = sorted(set(files))

    if not files:
        raise RuntimeError(
            "No video files (.mp4/.mov/.mkv) were downloaded from that Google Drive URL. "
            "Ensure the Drive link is shared as 'Anyone with the link'."
        )

    for path in files:
        fname = os.path.basename(path)
        if fname not in used_items:
            return path, fname

    return files[0], os.path.basename(files[0])


def get_video_duration(video_path: str) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return max(10, int(float(res.stdout.strip())))


def analyze_video_with_gemini(video_path: str, duration_sec: int, notes: str) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)

    sample_audio = "drive_audio.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-b:a", "48k", sample_audio],
        check=True,
    )

    uploaded = client.files.upload(file=sample_audio)
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    clip_len = min(32, duration_sec)
    max_start = max(0, duration_sec - clip_len)
    brief_text = (
        notes
        or "Campaign: Fundingpips Clipping | Promo Code: FPMOVEMENT (20% off) | "
        "Highlight funded trading discipline, risk management, and mention code FPMOVEMENT for 20% off."
    )

    prompt = f"""
You are a viral short-form finance & trading editor for @capitaldecoded13.
Listen to this video's audio track (total duration {duration_sec}s) and pick ONE high-retention {clip_len}-second segment (start_sec between 0 and {max_start}).

Campaign Brief & Rules:
{brief_text}

Return ONLY valid JSON with these exact keys:
{{
  "start_sec": <integer start timestamp in seconds between 0 and {max_start}>,
  "duration_sec": <integer between {min(25, clip_len)} and {clip_len}>,
  "overlay_tag": "<short top-left disclosure badge under 28 chars, e.g. #ad | Code FPMOVEMENT>",
  "voiceover_script": "<punchy 60-75 word US English narration summarizing the key insight from this clip and following the campaign brief>",
  "title": "<YouTube Shorts title under 70 chars including #Shorts #ad>",
  "caption": "<TikTok/Shorts description with #ad #trading #finance and campaign promo code>"
}}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[uploaded, prompt],
        config={"response_mime_type": "application/json"},
    )
    data = json.loads(response.text)
    data["start_sec"] = max(0, min(max_start, int(data.get("start_sec", 0))))
    data["duration_sec"] = max(10, min(duration_sec, int(data.get("duration_sec", clip_len))))
    return data


async def generate_voiceover(text: str, out_mp3: str) -> None:
    communicate = edge_tts.Communicate(text, VOICE_NAME, rate="+6%")
    await communicate.save(out_mp3)


def render_vertical_clip(
    source_path: str,
    start_sec: int,
    duration_sec: int,
    overlay_tag: str,
    vo_mp3: str,
    final_mp4: str,
) -> None:
    safe_tag = overlay_tag.replace(":", "").replace("'", "").strip() or "#ad | Code FPMOVEMENT"
    vf_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"drawtext=text='{safe_tag}':fontcolor=white:fontsize=34:"
        "box=1:boxcolor=black@0.55:boxborderw=10:x=40:y=60"
    )
    ff_cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_sec),
        "-t",
        str(duration_sec),
        "-i",
        source_path,
        "-i",
        vo_mp3,
        "-filter_complex",
        f"[0:v]{vf_filter}[v]",
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        final_mp4,
    ]
    subprocess.run(ff_cmd, check=True)


def upload_to_youtube(youtube, video_path: str, title: str, description: str) -> str:
    body = {
        "snippet": {
            "title": title[:95],
            "description": description,
            "tags": ["Shorts", "ad", "trading", "finance", "propfirm"],
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    return f"https://youtube.com/shorts/{resp.get('id', '')}"


def upload_to_tiktok_inbox(video_path: str) -> str:
    if not TIKTOK_ACCESS_TOKEN:
        return "Skipped (TIKTOK_ACCESS_TOKEN not set)"

    file_size = os.path.getsize(video_path)
    headers = {
        "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    init_res = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers=headers,
        json={
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            }
        },
        timeout=30,
    ).json()

    data = init_res.get("data", {})
    upload_url = data.get("upload_url")
    if not upload_url:
        return f"TikTok Init Warning: {init_res}"

    with open(video_path, "rb") as vf:
        put_res = requests.put(
            upload_url,
            headers={
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
                "Content-Type": "video/mp4",
            },
            data=vf.read(),
            timeout=120,
        )
    if put_res.status_code in (200, 201):
        return f"Uploaded to Creator Inbox (publish_id: {data.get('publish_id', '')})"
    return f"TikTok Upload HTTP {put_res.status_code}: {put_res.text}"


def main() -> None:
    used_items = load_used_items()
    source_path, item_key = download_from_google_drive(VIDEO_URL, used_items)
    total_duration = get_video_duration(source_path)
    print(f"Downloaded from Google Drive: {item_key} ({total_duration}s)")

    plan = analyze_video_with_gemini(source_path, total_duration, CAMPAIGN_NOTES)
    print(f"Gemini selected {plan['start_sec']}s for {plan['duration_sec']}s")

    vo_file = "voiceover.mp3"
    final_file = "final_short.mp4"
    asyncio.run(generate_voiceover(plan["voiceover_script"], vo_file))
    render_vertical_clip(
        source_path,
        plan["start_sec"],
        plan["duration_sec"],
        plan.get("overlay_tag", "#ad | Code FPMOVEMENT"),
        vo_file,
        final_file,
    )

    youtube = get_youtube_upload_client()
    yt_url = upload_to_youtube(youtube, final_file, plan["title"], plan["caption"])
    tt_status = upload_to_tiktok_inbox(final_file)

    save_used_item(item_key)

    summary = f"""
=== RUN COMPLETE ===
Drive Source: {item_key}
YouTube Shorts: {yt_url}
TikTok Status: {tt_status}
Caption: {plan['caption']}
"""
    print(summary)


if __name__ == "__main__":
    main()
