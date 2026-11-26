import os
import re
import glob
import json
import asyncio
import subprocess
import requests
import edge_tts
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

CAMPAIGN_URL = os.environ["CAMPAIGN_URL"].strip()
EXTRA_BRIEF = os.environ.get("EXTRA_BRIEF", "").strip()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# 1. Extract Campaign ID from URL & load or auto-register campaign rules
match = re.search(r"campaigns/([a-zA-Z0-9-]+)", CAMPAIGN_URL)
campaign_id = match.group(1) if match else CAMPAIGN_URL

with open("campaigns_db.json", "r", encoding="utf-8") as f:
    db = json.load(f)

if campaign_id in db:
    cfg = db[campaign_id]
else:
    # Auto-extract rules via Gemini if you pasted a new campaign's brief text
    parse_prompt = f"""
    Extract campaign details into valid JSON with keys:
    "brand", "youtube_handles" (list of official YouTube channel /videos URLs), "promo_code", "discount_text", "mention", "hashtag", "min_seconds" (int, default 22).
    Campaign URL: {CAMPAIGN_URL}
    Brief Notes: {EXTRA_BRIEF}
    """
    cfg = json.loads(client.models.generate_content(
        model="gemini-2.5-flash",
        contents=parse_prompt,
        config={"response_mime_type": "application/json"}
    ).text)
    db[campaign_id] = cfg
    with open("campaigns_db.json", "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)

# 2. Auto-discover latest unused YouTube video from the brand's official channel
used_ids = set()
if os.path.exists("used_videos.txt"):
    with open("used_videos.txt", "r", encoding="utf-8") as f:
        used_ids = {line.strip() for line in f if line.strip()}

target_video_url = None
target_video_id = None

for feed_url in cfg["youtube_handles"]:
    res = subprocess.run([
        "yt-dlp", "--flat-playlist", "--playlist-end", "15",
        "--print", "%(id)s|%(duration)s|%(title)s",
        feed_url
    ], capture_output=True, text=True)
    for line in res.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        vid_id, dur_str = parts[0].strip(), parts[1].strip()
        try:
            dur = float(dur_str)
        except ValueError:
            dur = 60.0
        # Pick videos >= 45s that haven't been clipped yet
        if vid_id not in used_ids and dur >= 45:
            target_video_id = vid_id
            target_video_url = f"https://www.youtube.com/watch?v={vid_id}"
            break
    if target_video_url:
        break

if not target_video_url:
    raise RuntimeError("No unused source videos found on brand channel—add a backup YouTube URL to campaigns_db.json.")

print(f"Auto-selected source video: {target_video_url}")

# 3. Fetch ONLY text transcript (0 MB video download) to pick best 30-second moment
subprocess.run([
    "yt-dlp", "--write-auto-sub", "--write-sub",
    "--sub-lang", "en", "--skip-download",
    "-o", "transcript", target_video_url
], check=False)

sub_files = glob.glob("transcript*.vtt")
transcript_text = ""
if sub_files:
    with open(sub_files[0], "r", encoding="utf-8", errors="ignore") as f:
        transcript_text = f.read()[:28000]

analysis_prompt = f"""
You are a viral finance & prop-trading editor for {cfg['brand']}.
Pick the single highest-retention 28 to 32 second window from this transcript (avoid intros/outros; if transcript is empty use "00:01:15" to "00:01:45").
Return ONLY valid JSON with:
- "start": "HH:MM:SS"
- "end": "HH:MM:SS" (must be 28-32 seconds after start)
- "title": punchy 50-char YouTube Shorts title (no quotes, no hashtags)
- "voiceover": 3-sentence authoritative US-English voiceover script (50-58 words) summarizing the trading/payout insight, ending naturally with: Use code {" ".join(cfg['promo_code'])} for twenty percent off.

Transcript:
{transcript_text}
"""

plan = json.loads(client.models.generate_content(
    model="gemini-2.5-flash",
    contents=analysis_prompt,
    config={"response_mime_type": "application/json"}
).text)

start_ts = plan.get("start", "00:01:15")
end_ts = plan.get("end", "00:01:45")
raw_title = plan.get("title", f"How {cfg['brand']} Traders Scale Capital")[:75]
voiceover_script = plan["voiceover"]

# 4. Slice ONLY those 30 seconds
subprocess.run([
    "yt-dlp",
    "--download-sections", f"*{start_ts}-{end_ts}",
    "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "--merge-output-format", "mp4",
    "-o", "raw_slice.mp4",
    target_video_url
], check=True)

# 5. Generate crisp US male voiceover + synced captions (Free edge-tts)
async def build_tts():
    comm = edge_tts.Communicate(voiceover_script, "en-US-BrianNeural", rate="+5%")
    subs = edge_tts.SubMaker()
    with open("voiceover.mp3", "wb") as af:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                af.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                subs.feed(chunk)
    with open("captions.srt", "w", encoding="utf-8") as sf:
        sf.write(subs.get_srt())

asyncio.run(build_tts())

# 6. Mute original speaker, crop 9:16, add #ad + yellow captions
vf_filter = (
    "crop=ih*9/16:ih,scale=1080:1920,"
    "eq=contrast=1.04:saturation=1.05,"
    "drawtext=text='#ad':fontcolor=white:fontsize=36:x=40:y=80:enable='between(t,0,3)',"
    "subtitles=captions.srt:force_style='Alignment=2,MarginV=340,Fontsize=18,PrimaryColour=&H0000FFFF,Outline=2'"
)

subprocess.run([
    "ffmpeg", "-y",
    "-i", "raw_slice.mp4", "-i", "voiceover.mp3",
    "-map", "0:v:0", "-map", "1:a:0",
    "-vf", vf_filter,
    "-c:v", "libx264", "-preset", "fast", "-crf", "22",
    "-c:a", "aac", "-b:a", "128k",
    "-shortest", "final_short.mp4"
], check=True)

caption_text = f"{raw_title}. {cfg['discount_text']} {cfg['mention']} {cfg['hashtag']} #ad #trading #propfirm"

# 7. Publish Publicly to YouTube Shorts
yt_url = "Not configured"
if os.environ.get("YT_REFRESH_TOKEN"):
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"]
    )
    youtube = build("youtube", "v3", credentials=creds)
    yt_res = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": f"{raw_title} #Shorts",
                "description": caption_text,
                "categoryId": "27"
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
        },
        media_body=MediaFileUpload("final_short.mp4", chunksize=-1, resumable=True)
    ).execute()
    yt_url = f"https://youtube.com/shorts/{yt_res['id']}"

# 8. Upload to TikTok Creator Inbox (Zero audit required)
tiktok_status = "Not configured"
if os.environ.get("TIKTOK_ACCESS_TOKEN"):
    fsize = os.path.getsize("final_short.mp4")
    init_res = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        headers={
            "Authorization": f"Bearer {os.environ['TIKTOK_ACCESS_TOKEN']}",
            "Content-Type": "application/json; charset=utf-8"
        },
        json={"source_info": {"source": "FILE_UPLOAD", "video_size": fsize, "chunk_size": fsize, "total_chunk_count": 1}}
    ).json()
    upload_url = init_res.get("data", {}).get("upload_url")
    if upload_url:
        with open("final_short.mp4", "rb") as vf:
            put_res = requests.put(
                upload_url,
                headers={"Content-Range": f"bytes 0-{fsize - 1}/{fsize}", "Content-Length": str(fsize), "Content-Type": "video/mp4"},
                data=vf
            )
        tiktok_status = f"Sent to TikTok Inbox (HTTP {put_res.status_code})"
    else:
        tiktok_status = f"TikTok Error: {init_res}"

# 9. Log video ID so future runs automatically pick a new video
with open("used_videos.txt", "a", encoding="utf-8") as uf:
    uf.write(f"{target_video_id}\n")

summary = f"""
## Campaign Clip Published ({cfg['brand']})
- **Source Video:** {target_video_url} (`{start_ts}` → `{end_ts}`)
- **YouTube Shorts Live URL (Copy & Submit):** {yt_url}
- **TikTok Status:** {tiktok_status}

### Copy-Paste Caption for TikTok & Content Rewards:
```text
{caption_text}
