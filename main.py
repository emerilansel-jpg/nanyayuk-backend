"""
NanyaYuk Micro-Backend
Handles video listing for TikTok & Instagram using yt-dlp.
Also provides YouTube transcript fetching without API key.

API format matches what the Supabase Edge Function expects:
  GET /api/tiktok/videos?username={username}
  GET /api/instagram/videos?username={username}
  POST /api/transcript
"""

import os
import json
import subprocess
import tempfile
import re
import glob
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="NanyaYuk Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# yt-dlp helper
# ============================================================

def run_ytdlp(args: list[str], timeout: int = 180) -> str:
    """Run yt-dlp with given arguments and return stdout."""
    cmd = ["yt-dlp"] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise Exception(f"yt-dlp error: {result.stderr[:500]}")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise Exception(f"yt-dlp timed out after {timeout}s")


def extract_videos_from_channel(url: str, max_videos: int = 50) -> list[dict]:
    """
    Use yt-dlp --flat-playlist to get video metadata.
    Works for TikTok and Instagram profiles.
    """
    args = [
        "--flat-playlist",
        "--no-download",
        "-j",
        "--playlist-end", str(max_videos),
        "--no-warnings",
        "--extractor-retries", "3",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        url,
    ]

    try:
        output = run_ytdlp(args, timeout=180)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    videos = []
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            video_id = str(data.get("id", ""))
            title = data.get("title", "") or ""
            description = data.get("description", "") or ""

            # Parse dates
            upload_date = data.get("upload_date", "")
            timestamp = data.get("timestamp")
            if upload_date and len(upload_date) == 8:
                published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
            elif isinstance(timestamp, (int, float)):
                published_at = datetime.utcfromtimestamp(timestamp).isoformat() + "Z"
            else:
                published_at = datetime.utcnow().isoformat() + "Z"

            thumbnail = (
                data.get("thumbnail", "")
                or (data.get("thumbnails", [{}])[0].get("url", "") if data.get("thumbnails") else "")
            )

            videos.append({
                # Fields the Edge Function checks (flexible mapping):
                "id": video_id,
                "video_id": video_id,
                "title": title[:200],
                "desc": description[:500],           # TikTok-style
                "description": description[:500],     # Instagram-style
                "caption": title[:200] or description[:200],  # Instagram caption
                "published_at": published_at,
                "timestamp": timestamp or 0,
                "createTime": timestamp or 0,         # TikTok-style
                "thumbnail_url": thumbnail,
                "thumbnail": thumbnail,
            })
        except json.JSONDecodeError:
            continue

    return videos


# ============================================================
# API Endpoints — matching what the Edge Function calls
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "NanyaYuk Backend",
        "version": "1.0.0",
        "endpoints": [
            "GET /api/tiktok/videos?username={username}",
            "GET /api/instagram/videos?username={username}",
            "POST /api/transcript",
            "GET /health",
        ],
    }


@app.get("/health")
async def health():
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=10)
        ytdlp_version = result.stdout.strip()
    except Exception:
        ytdlp_version = "not available"
    return {
        "status": "ok",
        "yt_dlp_version": ytdlp_version,
        "groq_configured": bool(os.environ.get("GROQ_API_KEY", "")),
    }


@app.get("/api/tiktok/videos")
async def get_tiktok_videos(username: str = Query(..., description="TikTok username")):
    """
    Fetch videos from a TikTok profile.
    Called by Edge Function: GET /api/tiktok/videos?username={username}
    Returns: { videos: [...] }
    """
    urls_to_try = [
        f"https://www.tiktok.com/@{username}",
        f"https://www.tiktok.com/@{username}/video",
    ]
    last_error = None
    for url in urls_to_try:
        try:
            videos = extract_videos_from_channel(url, max_videos=50)
            if videos:
                return {"videos": videos}
        except HTTPException as e:
            last_error = e
            continue

    try:
        videos = await _fetch_tiktok_via_oembed(username)
        if videos:
            return {"videos": videos}
    except Exception:
        pass

    if last_error:
        return {"videos": [], "error": f"TikTok extraction failed for @{username}. TikTok may be blocking server-side requests."}
    return {"videos": []}


async def _fetch_tiktok_via_oembed(username: str) -> list[dict]:
    """Fallback: use TikTok's oembed/public API to get basic video info."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://www.tiktok.com/api/user/detail/?uniqueId={username}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                user_info = data.get("userInfo", {}).get("user", {})
                if user_info.get("id"):
                    sec_uid = user_info.get("secUid", "")
                    if sec_uid:
                        videos = extract_videos_from_channel(
                            f"https://www.tiktok.com/@{username}", max_videos=30
                        )
                        return videos
    except Exception:
        pass
    return []


@app.get("/api/instagram/videos")
async def get_instagram_videos(username: str = Query(..., description="Instagram username")):
    """
    Fetch reels/videos from an Instagram profile.
    Called by Edge Function: GET /api/instagram/videos?username={username}
    Returns: { videos: [...] }
    """
    urls_to_try = [
        f"https://www.instagram.com/{username}/reels/",
        f"https://www.instagram.com/{username}/",
    ]
    last_error = None
    for url in urls_to_try:
        try:
            videos = extract_videos_from_channel(url, max_videos=50)
            if videos:
                return {"videos": videos}
        except HTTPException as e:
            last_error = e
            continue

    if last_error:
        return {"videos": [], "error": f"Instagram extraction failed for @{username}. Instagram may require authentication for server-side requests."}
    return {"videos": []}


# ============================================================
# Bonus: unified endpoint (for direct use)
# ============================================================

@app.post("/api/channel-videos")
async def get_channel_videos_unified(req: dict):
    """Unified endpoint — accepts {channel_url, platform, max_videos}."""
    channel_url = req.get("channel_url", "")
    platform = req.get("platform", "").lower()
    max_videos = req.get("max_videos", 50)

    if not channel_url:
        raise HTTPException(status_code=400, detail="channel_url required")

    videos = extract_videos_from_channel(channel_url, max_videos)
    return {"success": True, "platform": platform, "video_count": len(videos), "videos": videos}


# ============================================================
# YouTube Transcript (no API key needed)
# ============================================================

class TranscriptRequest(BaseModel):
    video_url: str
    video_id: Optional[str] = None
    platform: str = "youtube"


@app.post("/api/transcript")
async def get_transcript(req: TranscriptRequest):
    """Get transcript for a video. YouTube uses existing captions (free)."""
    platform = req.platform.lower()

    if platform == "youtube":
        video_id = req.video_id
        if not video_id:
            match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', req.video_url)
            if match:
                video_id = match.group(1)
            else:
                raise HTTPException(status_code=400, detail="Could not extract YouTube video ID")
        result = _fetch_youtube_transcript(video_id)
        return {"success": True, **result}

    elif platform in ("tiktok", "instagram"):
        result = await _transcribe_video(req.video_url)
        return {"success": not bool(result.get("error")), **result}

    raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")


def _fetch_youtube_transcript(video_id: str) -> dict:
    """Fetch YouTube captions using yt-dlp subtitle extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for sub_args in [
            ["--write-auto-sub", "--sub-lang", "en,id", "--sub-format", "json3"],
            ["--write-sub", "--sub-lang", "en,id", "--sub-format", "json3"],
            ["--write-auto-sub", "--sub-lang", "en,id", "--sub-format", "vtt"],
        ]:
            try:
                run_ytdlp([
                    "--skip-download", *sub_args,
                    "--output", f"{tmpdir}/%(id)s.%(ext)s",
                    f"https://www.youtube.com/watch?v={video_id}",
                ], timeout=60)
            except Exception:
                continue

            sub_files = glob.glob(f"{tmpdir}/*.json3") or glob.glob(f"{tmpdir}/*.vtt")
            if sub_files:
                return _parse_subtitle_file(sub_files[0])

        return {"transcript": "", "language": "", "error": "No subtitles available"}


def _parse_subtitle_file(filepath: str) -> dict:
    """Parse json3 or vtt subtitle file into transcript text."""
    lang = "en" if ".en." in filepath else "id" if ".id." in filepath else "unknown"
    with open(filepath) as f:
        content = f.read()

    if filepath.endswith(".json3"):
        try:
            sub_data = json.loads(content)
            segments = []
            for event in sub_data.get("events", []):
                text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
                if text.strip():
                    segments.append({"start": event.get("tStartMs", 0) / 1000, "text": text.strip()})
            return {"transcript": " ".join(s["text"] for s in segments), "segments": segments, "language": lang}
        except json.JSONDecodeError:
            return {"transcript": content, "language": lang}
    else:
        lines = [
            re.sub(r'<[^>]+>', '', line).strip()
            for line in content.split("\n")
            if line.strip() and not line.startswith("WEBVTT") and "-->" not in line and not line.strip().isdigit()
        ]
        return {"transcript": " ".join(lines), "language": lang}


# ============================================================
# TikTok/Instagram transcript via Groq Whisper (optional)
# ============================================================

async def _transcribe_video(video_url: str) -> dict:
    """Download audio → Groq Whisper API for transcription."""
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return {"transcript": "", "error": "GROQ_API_KEY not set"}

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = f"{tmpdir}/audio.mp3"
        try:
            run_ytdlp([
                "-x", "--audio-format", "mp3", "--audio-quality", "9",
                "--output", audio_path, "--max-filesize", "25M", video_url,
            ], timeout=120)
        except Exception as e:
            return {"transcript": "", "error": f"Audio download failed: {e}"}

        if not os.path.exists(audio_path):
            audio_files = glob.glob(f"{tmpdir}/audio.*")
            audio_path = audio_files[0] if audio_files else None
        if not audio_path or not os.path.exists(audio_path):
            return {"transcript": "", "error": "Audio file not created"}

        import httpx
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with open(audio_path, "rb") as f:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {groq_key}"},
                        files={"file": ("audio.mp3", f, "audio/mpeg")},
                        data={"model": "whisper-large-v3", "response_format": "verbose_json"},
                    )
                if resp.status_code != 200:
                    return {"transcript": "", "error": f"Groq API error: {resp.status_code}"}
                result = resp.json()
                return {
                    "transcript": result.get("text", ""),
                    "segments": [{"start": s.get("start", 0), "text": s.get("text", "")} for s in result.get("segments", [])],
                    "language": result.get("language", ""),
                }
        except Exception as e:
            return {"transcript": "", "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
