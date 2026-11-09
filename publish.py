#!/usr/bin/env python3
"""Send queued posts whose slot has come to the creator's TikTok inbox. Standard library only.

Reads the queue from the gh-pages checkout (`--site`): q/<id>/item.json + slides, written by the
laptop (media-agent `QueuePublisher`). TikTok pulls the slides from GitHub Pages, so PAGES_URL
must be the URL prefix verified in the TikTok developer portal.

Mode is MEDIA_UPLOAD: the post lands in the TikTok inbox and the creator picks a sound and posts.
Direct Post is not used (an unaudited app can only direct-post privately).

State (`--state`, committed on main) records every item that was sent, failed or skipped, so a
post is never sent twice. An item still due but not sendable yet (Pages not deployed, TikTok cap)
is simply tried again on the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://open.tiktokapis.com"
LATE_HOURS = 12  # a slot missed by more than this is skipped, not sent late
STATUS_WAIT_S = 90
INIT_GAP_S = 11  # TikTok allows six init requests per minute per token
KEEP_STATE_DAYS = 60
# Codes worth retrying on a later run rather than giving up on the post.
RETRY_CODES = {
    "spam_risk_too_many_pending_share",
    "spam_risk_too_many_posts",
    "rate_limit_exceeded",
    "internal_error",
    "url_ownership_unverified",
    "access_token_invalid",
    "reached_active_user_cap",
}


# ---------------------------------------------------------------------------- pure logic


def load_items(site: Path) -> list[dict]:
    items = []
    for meta in sorted((site / "q").glob("*/item.json")):
        try:
            items.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"skipping unreadable {meta}: {exc}")
    return items


def due_items(items: list[dict], sent: dict, now: datetime, force: str | None = None) -> tuple[list, list]:
    """(due, late): items to send now, and items whose slot passed more than LATE_HOURS ago."""
    due, late = [], []
    for it in items:
        if it["id"] in sent:
            continue
        if force:
            if it["id"] == force:
                due.append(it)
            continue
        slot = datetime.fromisoformat(it["slot"])
        if slot > now:
            continue
        (late if now - slot > timedelta(hours=LATE_HOURS) else due).append(it)
    due.sort(key=lambda it: datetime.fromisoformat(it["slot"]))
    return due, late


def trim_state(sent: dict, now: datetime) -> dict:
    keep = {}
    for k, v in sent.items():
        try:
            at = datetime.fromisoformat(v["at"])
        except (KeyError, ValueError, TypeError):
            keep[k] = v
            continue
        if now - at <= timedelta(days=KEEP_STATE_DAYS):
            keep[k] = v
    return keep


# ---------------------------------------------------------------------------- I/O


def http(method: str, url: str, data: dict | None = None, headers: dict | None = None, form: bool = False):
    body, h = None, dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw.decode(errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": {"code": "internal_error", "message": str(exc)}}


def url_ok(url: str) -> bool:
    """TikTok does not follow redirects: the slide must answer 200 at exactly this URL."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as resp:
            return resp.status == 200 and resp.url == url
    except (urllib.error.URLError, TimeoutError):
        return False


def notify(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    http("POST", f"https://api.telegram.org/bot{token}/sendMessage",
         {"chat_id": chat, "text": text, "disable_web_page_preview": True})


def save_refresh_token(new: str) -> None:
    """TikTok may rotate the refresh token. Store the new one as the repo secret (needs GH_PAT)."""
    repo = os.environ.get("REPO")
    if not (os.environ.get("GH_TOKEN") and repo):
        print("WARNING: TikTok issued a new refresh token but GH_PAT is not set; it was not saved.")
        notify("publish-queue: TikTok rotated its refresh token and GH_PAT is missing. Run `carousel tiktok auth` again.")
        return
    res = subprocess.run(["gh", "secret", "set", "TIKTOK_REFRESH_TOKEN", "--repo", repo],
                         input=new, text=True, capture_output=True)
    print("refresh token rotated and saved" if res.returncode == 0 else f"WARNING: could not save token: {res.stderr}")


def access_token() -> str:
    old = os.environ["TIKTOK_REFRESH_TOKEN"]
    _, j = http("POST", f"{API}/v2/oauth/token/", {
        "client_key": os.environ["TIKTOK_CLIENT_KEY"],
        "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": old,
    }, form=True)
    if "access_token" not in j:
        raise RuntimeError(f"TikTok token refresh failed: {j.get('error')} {j.get('error_description')}")
    new = j.get("refresh_token")
    if new and new != old:
        save_refresh_token(new)
    return j["access_token"]


def send(item: dict, token: str, pages_url: str) -> tuple[str, str, str | None]:
    """Returns (outcome, detail, publish_id); outcome is sent | retry | failed."""
    urls = [pages_url.rstrip("/") + "/" + p for p in item["images"]]
    missing = [u for u in urls if not url_ok(u)]
    if missing:
        return "retry", f"{len(missing)} slide(s) not on GitHub Pages yet, e.g. {missing[0]}", None
    post_info = {"description": item.get("description", "")}
    if item.get("title"):
        post_info["title"] = item["title"]
    _, j = http("POST", f"{API}/v2/post/publish/content/init/", {
        "post_info": post_info,
        "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": item.get("cover_index", 0),
                        "photo_images": urls},
        "post_mode": "MEDIA_UPLOAD",
        "media_type": "PHOTO",
    }, headers={"Authorization": f"Bearer {token}"})
    err = j.get("error") or {}
    code = err.get("code")
    if code != "ok":
        detail = f"{code}: {err.get('message', '')}".strip()
        return ("retry" if code in RETRY_CODES else "failed"), detail, None
    pid = j["data"]["publish_id"]
    deadline = time.monotonic() + STATUS_WAIT_S
    status = "PROCESSING_DOWNLOAD"
    while time.monotonic() < deadline:
        time.sleep(6)
        _, s = http("POST", f"{API}/v2/post/publish/status/fetch/", {"publish_id": pid},
                    headers={"Authorization": f"Bearer {token}"})
        data = s.get("data") or {}
        status = data.get("status", status)
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
        if status == "FAILED":
            return "failed", f"TikTok failed it: {data.get('fail_reason')}", pid
    # Still processing counts as sent: the request was accepted, and sending again would duplicate.
    return "sent", status, pid


# ---------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=Path, default=Path("site"))
    ap.add_argument("--state", type=Path, default=Path("state/sent.json"))
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    sent = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    items = load_items(args.site) if (args.site / "q").is_dir() else []
    due, late = due_items(items, sent, now, force=os.environ.get("FORCE_ITEM") or None)

    for it in late:
        sent[it["id"]] = {"status": "skipped_late", "at": stamp}
        print(f"{it['id']}: skipped, slot {it['slot']} passed more than {LATE_HOURS} h ago")
        notify(f"Skipped {it['id']}: its slot ({it['slot']}) passed more than {LATE_HOURS} h before it could be sent.")

    failures = 0
    if due:
        missing = [k for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN", "PAGES_URL")
                   if not os.environ.get(k)]
        if missing:
            print(f"{len(due)} item(s) due but not configured: missing {', '.join(missing)}")
            failures = 1
        else:
            token = access_token()
            for i, it in enumerate(due):
                if i:
                    time.sleep(INIT_GAP_S)
                outcome, detail, pid = send(it, token, os.environ["PAGES_URL"])
                print(f"{it['id']}: {outcome} - {detail}")
                if outcome == "sent":
                    sent[it["id"]] = {"status": detail, "publish_id": pid, "at": stamp}
                    hint = f"\nSuggested sound: {it['sound_hint']}" if it.get("sound_hint") else ""
                    notify(f"{it['id']} is in your TikTok inbox. Open TikTok, pick a sound and post it.{hint}")
                elif outcome == "failed":
                    failures += 1
                    sent[it["id"]] = {"status": "failed", "error": detail, "publish_id": pid, "at": stamp}
                    notify(f"{it['id']} could not be sent to TikTok: {detail}")
    else:
        print("nothing due")

    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(trim_state(sent, now), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
