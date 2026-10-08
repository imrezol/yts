"""Download metadata for every video on a YouTube channel and store it in MongoDB."""

import argparse
import logging
import os
from datetime import datetime, timezone

import yt_dlp
from pymongo import MongoClient

log = logging.getLogger("yts")

# Large, mostly-useless-for-analysis fields dropped unless --keep-formats is given.
HEAVY_FIELDS = ("formats", "requested_formats", "automatic_captions", "thumbnails", "heatmap")


def list_video_ids(channel_url):
    """Yield (video_id, url) for every video on the channel, including all tabs."""
    opts = {"extract_flat": "in_playlist", "quiet": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        seen = set()
        stack = [channel_url]
        while stack:
            info = ydl.extract_info(stack.pop(), download=False)
            if not info:
                continue
            for entry in info.get("entries") or []:
                if not entry:
                    continue
                # A channel root returns its tabs (Videos, Shorts, Live) as nested playlists.
                if entry.get("_type") == "playlist" or entry.get("ie_key") == "YoutubeTab":
                    stack.append(entry["url"])
                    continue
                video_id = entry.get("id")
                if video_id and video_id not in seen:
                    seen.add(video_id)
                    yield video_id, entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"


def fetch_metadata(ydl, url, keep_formats):
    info = ydl.extract_info(url, download=False)
    if not info:
        return None
    info = ydl.sanitize_info(info)
    if not keep_formats:
        for field in HEAVY_FIELDS:
            info.pop(field, None)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Channel URL, e.g. https://www.youtube.com/@handle")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    parser.add_argument("--db", default=os.getenv("MONGO_DB", "youtube"))
    parser.add_argument("--collection", default=os.getenv("MONGO_COLLECTION", "videos"))
    parser.add_argument("--refresh", action="store_true", help="Re-fetch videos already stored")
    parser.add_argument("--keep-formats", action="store_true", help="Keep formats/captions/thumbnails")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    collection = MongoClient(args.mongo_uri)[args.db][args.collection]
    collection.create_index("channel_id")
    collection.create_index("upload_date")

    stored = 0
    skipped = 0
    failed = 0
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "ignoreerrors": True}) as ydl:
        for video_id, url in list_video_ids(args.channel):
            if not args.refresh and collection.count_documents({"_id": video_id}, limit=1):
                skipped += 1
                continue
            try:
                info = fetch_metadata(ydl, url, args.keep_formats)
            except yt_dlp.utils.DownloadError as e:
                info = None
                log.warning("%s: %s", video_id, e)
            if info is None:
                failed += 1
                continue
            info["_id"] = video_id
            info["fetched_at"] = datetime.now(timezone.utc)
            collection.replace_one({"_id": video_id}, info, upsert=True)
            stored += 1
            log.info("stored %s %s", video_id, info.get("title"))

    log.info("done: %d stored, %d skipped (already present), %d failed", stored, skipped, failed)


if __name__ == "__main__":
    main()
