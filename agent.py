import os
import re
import json
import asyncio
import subprocess
import requests
import edge_tts
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# 1. Load environment inputs & secrets
CAMPAIGN_URL = os.environ.get("CAMPAIGN_URL", "").strip()
EXTRA_BRIEF_NOTES = os.environ.get("EXTRA_BRIEF_NOTES", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()
TIKTOK_ACCESS_TOKEN = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()

USED_VIDEOS_FILE = "used_videos.txt"
CAMPAIGNS_DB_FILE = "campaigns_db.json"
VOICE_NAME = "en-US-BrianNeural"


def load_used_videos() -> set:
    if not os.path.exists(USED_VIDEOS_FILE):
        return set()
    with open(USED_VIDEOS_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_used_video(video_id: str) -> None:
    with open(USED_VIDEOS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\n")


def load_campaign_config(campaign_url: str, extra_notes: str) -> dict:
    db = {}
    if os.path.exists(CAMPAIGNS_DB_FILE):
        with open(CAMPAIGNS_DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            db = raw if isinstance(raw, dict) else {c.get("url", ""): c for c in raw}

    for key, val in db.items():
        if key and (key in campaign_url or campaign_url in key):
            cfg = dict(val)
            if extra_notes:
                cfg["brief"] = f"{cfg.get('brief', '')}\n{extra_notes}".strip()
            return cfg

    # Fallback if a brand-new campaign URL is pasted with brief notes
    return {
        "name": "Fundingpips Clipping",
        "promo_code": "FPMOVEMENT",
        "discount": "20% off",
        "search_queries": [
            "prop firm funded trader payout interview podcast",
            "forex funded trader strategy podcast",
        ],
        "brief": extra_notes
        or "Highlight funded trading discipline, risk management, and mention code FPMOVEMENT for 20% off.",
    }


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:v=|\/shorts\/|youtu\.be\/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def find_candidate_video(campaign_url: str, cfg: dict, used_ids: set) -> tuple[str, str]:
    direct_id = extract_video_id(campaign_url)
    if direct_id:
        return direct_id, f"https://www.youtube.com/watch?v={direct_id}"

    queries = cfg.get("search_queries") or [
        "prop firm funded trader interview podcast",
        "day trading risk management podcast",
    ]
    for query in queries:
        cmd = [
            "yt-dlp",
            f"ytsearch10:{query}",
            "--print",
            "%(id)s|%(duration)s",
            "--no-warnings",
            "--ignore-errors",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) != 2:
                continue
            vid, dur_str = parts
            if vid in used_ids or not dur_str.isdigit():
                continue
            dur = int(dur_str)
            # Prefer long-form podcast/interview videos (5 min to 3 hrs)
            if 300 <= dur <= 10800:
                return vid, f"https://www.youtube.com/watch?v={vid}"

    raise RuntimeError("No unused candidate YouTube videos found for campaign queries.")


def fetch_transcript(video_id: str) -> list[dict]:
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    return [
        {"start": item.start, "duration": item.duration, "text": item.text}
        for item in transcript
    ]


def analyze_with_gemini(transcript: list[dict], cfg: dict) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)
    compact_lines = [
        f"[{int(seg['start'])}s] {seg['text']}"
        for seg in transcript
        if seg["start"] <= 3600
    ]
    transcript_block = "\n".join(compact_lines[:450])

    prompt = f"""
You are a viral short-form finance & trading editor for @capitaldecoded13.
Analyze the podcast transcript below and pick ONE high-retention 28-to-35 second segment.

Campaign: {cfg.get('name', 'Fundingpips Clipping')}
Promo Code: {cfg.get('promo_code', 'FPMOVEMENT')} ({cfg.get('discount', '20% off')})
Brief Rules: {cfg.get('brief', '')}

Return ONLY valid JSON with these exact keys:
{{
  "start_sec": <integer start timestamp in seconds>,
  "duration_sec": <integer between 28 and 35>,
  "voiceover_script": "<punchy 65-75 word US English narration summarizing the insight and naturally mentioning code {cfg.get('promo_code', 'FPMOVEMENT')} for {cfg.get('discount', '20% off')}>",
  "title": "<YouTube Shorts title under 70 chars including #Shorts #ad>",
  "caption": "<TikTok/Shorts description with #ad #trading #propfirm #finance and promo code {cfg.get('promo_code', 'FPMOVEMENT')}>"
}}

Transcript:
{transcript_block}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    data = json.loads(response.text)
    data["start_sec"] = max(0, int(data.get("start_sec", 60)))
    data["duration_sec"] = min(40, max(25, int(data.get("duration_sec", 30))))
    return data


async def generate_voiceover(text: str, out_mp3: str) -> None:
    communicate = edge_tts.Communicate(text, VOICE_NAME, rate="+6%")
    await communicate.save(out_mp3)


def download_and_render_clip(
    video_url: str, start_sec: int, duration_sec: int, vo_mp3: str, final_mp4: str
) -> None:
    raw_clip = "raw_slice.mp4"
    end_sec = start_sec + duration_sec

    dl_cmd = [
        "yt-dlp",
        "-f",
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--download-sections",
        f"*{start_sec}-{end_sec}",
        "--force-keyframes-at-cuts",
        "-o",
        raw_clip,
        video_url,
    ]
    subprocess.run(dl_cmd, check=True)

    # Vertical 1080x1920 crop, mute original audio, mix US voiceover, burn #ad disclosure
    vf_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "drawtext=text='#ad | Code\\: FPMOVEMENT':fontcolor=white:fontsize=34:"
        "box=1:boxcolor=black@0.55:boxborderw=10:x=40:y=60"
    )
    ff_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        raw_clip,
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


def upload_to_youtube(video_path: str, title: str, description: str) -> str:
    creds = Credentials(
        token=None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)

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
    vid = resp.get("id", "")
    return f"https://youtube.com/shorts/{vid}"


def upload_to_tiktok_inbox(video_path: str) -> str:
    if not TIKTOK_ACCESS_TOKEN:
        return "Skipped (TIKTOK_ACCESS_TOKEN not set)"

    file_size = os.path.getsize(video_path)
    headers = {
        "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    init_payload = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,
            "total_chunk_count": 1,
        }
    }
    init_res = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers=headers,
        json=init_payload,
        timeout=30,
    ).json()

    data = init_res.get("data", {})
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id", "")
    if not upload_url:
        return f"TikTok Init Warning: {init_res}"

    with open(video_path, "rb") as vf:
        video_bytes = vf.read()

    put_headers = {
        "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
        "Content-Type": "video/mp4",
    }
    put_res = requests.put(upload_url, headers=put_headers, data=video_bytes, timeout=120)
    if put_res.status_code in (200, 201):
        return f"Uploaded to Creator Inbox (publish_id: {publish_id})"
    return f"TikTok Upload HTTP {put_res.status_code}: {put_res.text}"


def main() -> None:
    if not CAMPAIGN_URL:
        raise ValueError("CAMPAIGN_URL environment variable is empty.")

    cfg = load_campaign_config(CAMPAIGN_URL, EXTRA_BRIEF_NOTES)
    used_ids = load_used_videos()

    video_id, video_url = find_candidate_video(CAMPAIGN_URL, cfg, used_ids)
    print(f"Selected source video: {video_url}")

    transcript = fetch_transcript(video_id)
    plan = analyze_with_gemini(transcript, cfg)
    print(f"Gemini selected {plan['start_sec']}s for {plan['duration_sec']}s")

    vo_file = "voiceover.mp3"
    final_file = "final_short.mp4"
    asyncio.run(generate_voiceover(plan["voiceover_script"], vo_file))
    download_and_render_clip(
        video_url, plan["start_sec"], plan["duration_sec"], vo_file, final_file
    )

    yt_url = upload_to_youtube(final_file, plan["title"], plan["caption"])
    tt_status = upload_to_tiktok_inbox(final_file)

    save_used_video(video_id)

    summary = f"""
=== RUN COMPLETE ===
Campaign: {cfg.get('name', 'Campaign')}
Source Video: {video_url}
YouTube Shorts: {yt_url}
TikTok Status: {tt_status}
Caption: {plan['caption']}
"""
    print(summary)


if __name__ == "__main__":
    main()
