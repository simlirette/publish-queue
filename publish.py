#!/usr/bin/env python3
"""Send queued posts whose slot has come: Instagram is published directly, TikTok goes to the inbox.
Standard library only.

The queue is the gh-pages checkout (`--site`): q/<id>/item.json plus its media, written by the
laptop (theblueprint-mag `QueuePublisher`, running-shadows `pnpm rs queue`). The platforms pull
the media from GitHub Pages, so PAGES_URL must be the URL prefix verified with TikTok.

item.json:
  id, account, platforms (["instagram", "tiktok"]; old items have platform: "tiktok"),
  kind "photo" (images, cover_index, alt_texts) or "video" (video, cover),
  slot (ISO with offset, or local time plus "timezone"), description, sound_hint, format.

Credentials (repo secrets; the workflow passes them all as SECRETS_JSON):
  TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
  TIKTOK_REFRESH_TOKEN (account "fashion") or TIKTOK_REFRESH_TOKEN_<ACCOUNT>,
  INSTAGRAM_TOKEN_<ACCOUNT> (long-lived Instagram Login token, refreshed here every run),
  GH_PAT (lets this script store rotated tokens), TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

State (`--state`, committed on main) records every platform delivery that was sent, failed or
skipped, keyed "<id>:<platform>", so nothing is sent twice. A delivery that cannot happen yet
(Pages not deployed, a platform limit) is tried again on the next run.

Analytics: once a day, every TikTok account with a token gets its own posts listed (scope
video.list) into state/analytics/<account>.json: views, likes, comments and shares per post, plus
the views of each day, so the content pipelines can learn which posts worked.
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
from zoneinfo import ZoneInfo

TIKTOK = "https://open.tiktokapis.com"
INSTAGRAM = "https://graph.instagram.com"
IG_VERSION = "v25.0"
LATE_HOURS = 12  # a slot missed by more than this is skipped, not sent late
TIKTOK_WAIT_S = 90
IG_WAIT_S = 300  # Reels can take a few minutes to process
INIT_GAP_S = 11  # TikTok allows six init requests per minute per token
KEEP_STATE_DAYS = 60
STATS_EVERY_H = 20  # one analytics pass a day
STATS_KEEP_DAYS = 120  # posts older than this leave the analytics file
STATS_FIELDS = ("id", "create_time", "title", "video_description", "duration", "view_count", "like_count",
                "comment_count", "share_count", "share_url")
TIKTOK_RETRY = {
    "spam_risk_too_many_pending_share", "spam_risk_too_many_posts", "rate_limit_exceeded",
    "internal_error", "url_ownership_unverified", "access_token_invalid", "reached_active_user_cap",
}
IG_RETRY_CODES = {1, 2, 4, 17, 32, 341, 613, -1}  # transient or rate limits


class Retry(Exception):
    """Not possible right now; try again on the next run."""


class Failed(Exception):
    """Will not work by retrying; record it and tell the user."""


# ---------------------------------------------------------------------------- items and state


def platforms(item: dict) -> list[str]:
    return list(item.get("platforms") or [item.get("platform", "tiktok")])


def slot_of(item: dict) -> datetime:
    slot = datetime.fromisoformat(item["slot"])
    if slot.tzinfo is None:
        slot = slot.replace(tzinfo=ZoneInfo(item.get("timezone") or "America/Toronto"))
    return slot


def key(item: dict, platform: str) -> str:
    return f"{item['id']}:{platform}"


def is_done(item: dict, platform: str, sent: dict) -> bool:
    # items queued before multi-platform support were recorded under their bare id (TikTok only)
    return key(item, platform) in sent or (platform == "tiktok" and item["id"] in sent)


def load_items(site: Path) -> list[dict]:
    items = []
    for meta in sorted((site / "q").glob("*/item.json")):
        try:
            items.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"skipping unreadable {meta}: {exc}")
    return items


def due_items(items: list[dict], sent: dict, now: datetime, force: str | None = None) -> tuple[list, list]:
    """(due, late) as (item, platform) pairs: due now, and missed by more than LATE_HOURS."""
    due, late = [], []
    for it in items:
        for platform in platforms(it):
            if is_done(it, platform, sent):
                continue
            if force:
                if it["id"] == force:
                    due.append((it, platform))
                continue
            slot = slot_of(it)
            if slot > now:
                continue
            (late if now - slot > timedelta(hours=LATE_HOURS) else due).append((it, platform))
    due.sort(key=lambda pair: slot_of(pair[0]))
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


# ---------------------------------------------------------------------------- I/O helpers


SECRETS: dict[str, str] = {}


def secret(name: str) -> str | None:
    return SECRETS.get(name) or os.environ.get(name) or None


def load_secrets() -> None:
    raw = os.environ.get("SECRETS_JSON")
    if raw:
        try:
            SECRETS.update({k: v for k, v in json.loads(raw).items() if isinstance(v, str)})
        except ValueError:
            print("WARNING: SECRETS_JSON is not valid JSON")


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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    """The platforms do not follow redirects: the file must answer 200 at exactly this URL."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as resp:
            return resp.status == 200 and resp.url == url
    except (urllib.error.URLError, TimeoutError):
        return False


def media_urls(item: dict, pages_url: str) -> dict:
    base = pages_url.rstrip("/") + "/"
    out = {"images": [base + p for p in item.get("images", [])]}
    if item.get("video"):
        out["video"] = base + item["video"]
    if item.get("cover"):
        out["cover"] = base + item["cover"]
    missing = [u for u in out["images"] + [out.get("video"), out.get("cover")] if u and not url_ok(u)]
    if missing:
        raise Retry(f"{len(missing)} file(s) not on GitHub Pages yet, e.g. {missing[0]}")
    return out


def notify(text: str) -> None:
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if token and chat:
        http("POST", f"https://api.telegram.org/bot{token}/sendMessage",
             {"chat_id": chat, "text": text, "disable_web_page_preview": True})


def save_secret(name: str, value: str) -> None:
    """Store a rotated token as a repo secret (needs GH_PAT)."""
    repo, pat = os.environ.get("REPO"), secret("GH_PAT")
    if not (repo and pat):
        print(f"WARNING: {name} was rotated but GH_PAT is not set; the new value was not saved.")
        notify(f"publish-queue: {name} was rotated and GH_PAT is missing. Sign in again for that account.")
        return
    res = subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value, text=True,
                         capture_output=True, env={**os.environ, "GH_TOKEN": pat})
    print(f"{name} rotated and saved" if res.returncode == 0 else f"WARNING: could not save {name}: {res.stderr}")


def env_suffix(account: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in account).upper()


# ---------------------------------------------------------------------------- TikTok (inbox)


def tiktok_token_name(account: str) -> str:
    return "TIKTOK_REFRESH_TOKEN" if account == "fashion" else f"TIKTOK_REFRESH_TOKEN_{env_suffix(account)}"


def tiktok_access_token(account: str) -> str:
    name = tiktok_token_name(account)
    old, ck, cs = secret(name), secret("TIKTOK_CLIENT_KEY"), secret("TIKTOK_CLIENT_SECRET")
    if not (old and ck and cs):
        raise Failed(f"TikTok is not set up for {account}: missing {name} or the client key/secret")
    _, j = http("POST", f"{TIKTOK}/v2/oauth/token/", {
        "client_key": ck, "client_secret": cs, "grant_type": "refresh_token", "refresh_token": old,
    }, form=True)
    if "access_token" not in j:
        raise Retry(f"TikTok token refresh failed for {account}: {j.get('error')} {j.get('error_description')}")
    new = j.get("refresh_token")
    if new and new != old:
        SECRETS[name] = new
        save_secret(name, new)
    return j["access_token"]


def tiktok_send(item: dict, token: str, urls: dict) -> str:
    auth = {"Authorization": f"Bearer {token}"}
    if item.get("kind") == "video":
        _, j = http("POST", f"{TIKTOK}/v2/post/publish/inbox/video/init/",
                    {"source_info": {"source": "PULL_FROM_URL", "video_url": urls["video"]}}, headers=auth)
    else:
        post_info = {"description": item.get("description", "")}
        if item.get("title"):
            post_info["title"] = item["title"]
        _, j = http("POST", f"{TIKTOK}/v2/post/publish/content/init/", {
            "post_info": post_info,
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": item.get("cover_index", 0),
                            "photo_images": urls["images"]},
            "post_mode": "MEDIA_UPLOAD",
            "media_type": "PHOTO",
        }, headers=auth)
    err = j.get("error") or {}
    if err.get("code") != "ok":
        detail = f"{err.get('code')}: {err.get('message', '')}".strip()
        raise (Retry if err.get("code") in TIKTOK_RETRY else Failed)(detail)
    pid = j["data"]["publish_id"]
    status, deadline = "PROCESSING_DOWNLOAD", time.monotonic() + TIKTOK_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(6)
        _, s = http("POST", f"{TIKTOK}/v2/post/publish/status/fetch/", {"publish_id": pid}, headers=auth)
        data = s.get("data") or {}
        status = data.get("status", status)
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
        if status == "FAILED":
            raise Failed(f"TikTok failed it: {data.get('fail_reason')}")
    # still processing counts as sent: the request was accepted and sending again would duplicate it
    return f"{status} {pid}"


# ---------------------------------------------------------------------------- Instagram (direct)


def ig_call(method: str, path: str, token: str, data: dict | None = None) -> dict:
    url = f"{INSTAGRAM}/{IG_VERSION}/{path}"
    if method == "GET":
        url += "?" + urllib.parse.urlencode({**(data or {}), "access_token": token})
        status, j = http("GET", url)
    else:
        status, j = http("POST", url, {**(data or {}), "access_token": token}, form=True)
    err = j.get("error") if isinstance(j.get("error"), dict) else None
    if err or status >= 400 or status == 0:
        err = err or {}
        detail = f"Instagram {err.get('code')}: {err.get('message') or j}"
        raise (Retry if err.get("code") in IG_RETRY_CODES or status in (0, 500, 502, 503) else Failed)(detail)
    return j


def ig_token(account: str) -> str:
    name = f"INSTAGRAM_TOKEN_{env_suffix(account)}"
    token = secret(name)
    if not token:
        raise Failed(f"Instagram is not set up for {account}: missing the {name} secret")
    # Refreshing keeps the 60-day token alive; Meta refuses tokens under 24 h old, which is fine.
    _, j = http("GET", f"{INSTAGRAM}/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": token}))
    new = j.get("access_token")
    if new and new != token:
        SECRETS[name] = new
        save_secret(name, new)
        token = new
    return token


def ig_wait(container: str, token: str) -> None:
    deadline = time.monotonic() + IG_WAIT_S
    while time.monotonic() < deadline:
        code = ig_call("GET", container, token, {"fields": "status_code"}).get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise Failed(f"Instagram could not process the media ({code})")
        time.sleep(8)
    raise Retry("Instagram is still processing the media; will publish on the next run")


def ig_image(user: str, token: str, url: str, alt: str, extra: dict) -> str:
    data = {"image_url": url, **extra}
    if alt:
        try:
            return ig_call("POST", f"{user}/media", token, {**data, "alt_text": alt})["id"]
        except Failed:
            pass  # alt text is a nicety; never lose a post over it
    return ig_call("POST", f"{user}/media", token, data)["id"]


def ig_send(item: dict, token: str, urls: dict) -> str:
    user = ig_call("GET", "me", token, {"fields": "user_id,username"})
    uid = user.get("user_id") or user.get("id")
    caption = item.get("description", "")
    if item.get("kind") == "video":
        data = {"media_type": "REELS", "video_url": urls["video"], "caption": caption, "share_to_feed": "true"}
        if urls.get("cover"):
            data["cover_url"] = urls["cover"]
        container = ig_call("POST", f"{uid}/media", token, data)["id"]
    else:
        images, alts = urls["images"], list(item.get("alt_texts") or [])
        alts += [""] * (len(images) - len(alts))
        if len(images) == 1:
            container = ig_image(uid, token, images[0], alts[0], {"caption": caption})
        else:
            children = [ig_image(uid, token, u, a, {"is_carousel_item": "true"}) for u, a in zip(images, alts)]
            for child in children:
                ig_wait(child, token)
            container = ig_call("POST", f"{uid}/media", token,
                                {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})["id"]
    ig_wait(container, token)
    media = ig_call("POST", f"{uid}/media_publish", token, {"creation_id": container})["id"]
    return f"published {media} on @{user.get('username', '?')}"


# ---------------------------------------------------------------------------- analytics (TikTok)


def tiktok_accounts() -> list[str]:
    """Every account that has a TikTok refresh token: "fashion" plus TIKTOK_REFRESH_TOKEN_<ACCOUNT>."""
    accounts = ["fashion"] if secret("TIKTOK_REFRESH_TOKEN") else []
    prefix = "TIKTOK_REFRESH_TOKEN_"
    names = set(SECRETS) | {k for k in os.environ if k.startswith(prefix)}
    accounts += sorted(n[len(prefix):].lower() for n in names if n.startswith(prefix) and secret(n))
    return accounts


def merge_stats(data: dict, videos: list[dict], now: datetime) -> dict:
    """Latest numbers per post, plus its views on each day (for growth curves)."""
    stamp, day = now.isoformat(timespec="seconds"), now.date().isoformat()
    posts = data.setdefault("posts", {})
    for v in videos:
        p = posts.setdefault(str(v["id"]), {"first_seen": stamp})
        p.update({k: v.get(k) for k in STATS_FIELDS if k != "id"}, updated_at=stamp)
        if v.get("view_count") is not None:
            p.setdefault("views_by_day", {})[day] = v["view_count"]
    cutoff = (now - timedelta(days=STATS_KEEP_DAYS)).timestamp()
    data["posts"] = {k: p for k, p in posts.items() if (p.get("create_time") or cutoff) >= cutoff}
    return data


def collect_stats(stats_dir: Path, now: datetime, tokens: dict) -> list[str]:
    notes = []
    for account in tiktok_accounts():
        path = stats_dir / f"{account}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        last = data.get("fetched_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=STATS_EVERY_H):
            continue
        data["fetched_at"] = now.isoformat(timespec="seconds")
        data.pop("last_error", None)
        try:
            if ("tiktok", account) not in tokens:
                tokens[("tiktok", account)] = tiktok_access_token(account)
            auth = {"Authorization": f"Bearer {tokens[('tiktok', account)]}"}
            videos, cursor = [], None
            for _ in range(10):  # 200 posts at most
                body = {"max_count": 20, **({"cursor": cursor} if cursor else {})}
                _, j = http("POST", f"{TIKTOK}/v2/video/list/?fields={','.join(STATS_FIELDS)}", body, headers=auth)
                err = j.get("error") or {}
                if err.get("code") != "ok":
                    raise Retry(f"{err.get('code')}: {err.get('message', '')}")
                d = j.get("data") or {}
                videos += d.get("videos") or []
                if not d.get("has_more"):
                    break
                cursor = d.get("cursor")
            merge_stats(data, videos, now)
            notes.append(f"analytics {account}: {len(videos)} posts")
        except (Retry, Failed) as exc:
            data["last_error"] = str(exc)
            notes.append(f"analytics {account}: not collected - {exc}")
        stats_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return notes


# ---------------------------------------------------------------------------- main


def deliver(item: dict, platform: str, pages_url: str, tokens: dict) -> str:
    urls = media_urls(item, pages_url)
    account = item.get("account", "fashion")
    if (platform, account) not in tokens:
        tokens[(platform, account)] = (tiktok_access_token if platform == "tiktok" else ig_token)(account)
    token = tokens[(platform, account)]
    if platform == "tiktok":
        return tiktok_send(item, token, urls)
    if platform == "instagram":
        return ig_send(item, token, urls)
    raise Failed(f"unknown platform {platform}")


def message(item: dict, platform: str) -> str:
    if platform == "instagram":
        return f"{item['id']} is live on Instagram."
    hint = f"\nSuggested sound: {item['sound_hint']}" if item.get("sound_hint") else ""
    return f"{item['id']} is in your TikTok inbox. Open TikTok, pick a sound and post it.{hint}"


def status_report(ids: str, account: str = "fashion") -> None:
    """Diagnostic: print TikTok's current status of publish ids (no secrets printed)."""
    token = tiktok_access_token(account)
    for pid in ids.split(","):
        _, s = http("POST", f"{TIKTOK}/v2/post/publish/status/fetch/", {"publish_id": pid.strip()},
                    headers={"Authorization": f"Bearer {token}"})
        print(f"status {pid.strip()[-8:]}: {json.dumps(s, ensure_ascii=False)}")
    _, u = http("GET", f"{TIKTOK}/v2/user/info/?fields=open_id,display_name", None,
                headers={"Authorization": f"Bearer {token}"})
    print(f"account {account}: {json.dumps(u, ensure_ascii=False)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=Path, default=Path("site"))
    ap.add_argument("--state", type=Path, default=Path("state/sent.json"))
    args = ap.parse_args()
    load_secrets()
    if os.environ.get("STATUS_IDS"):
        status_report(os.environ["STATUS_IDS"])
        return 0

    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    sent = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    items = load_items(args.site) if (args.site / "q").is_dir() else []
    due, late = due_items(items, sent, now, force=os.environ.get("FORCE_ITEM") or None)

    for it, platform in late:
        sent[key(it, platform)] = {"status": "skipped_late", "at": stamp}
        print(f"{it['id']} {platform}: skipped, slot {it['slot']} passed more than {LATE_HOURS} h ago")
        notify(f"Skipped {it['id']} on {platform}: its slot passed more than {LATE_HOURS} h before it could be sent.")

    failures = 0
    tokens: dict = {}
    pages_url = os.environ.get("PAGES_URL") or secret("PAGES_URL")
    if due and not pages_url:
        print(f"{len(due)} delivery(ies) due but PAGES_URL is not set")
        failures = 1
    elif due:
        tiktok_calls = 0
        for it, platform in due:
            if platform == "tiktok":
                if tiktok_calls:
                    time.sleep(INIT_GAP_S)
                tiktok_calls += 1
            try:
                detail = deliver(it, platform, pages_url, tokens)
            except Retry as exc:
                print(f"{it['id']} {platform}: will retry - {exc}")
                continue
            except Failed as exc:
                failures += 1
                sent[key(it, platform)] = {"status": "failed", "error": str(exc), "at": stamp}
                print(f"{it['id']} {platform}: FAILED - {exc}")
                notify(f"{it['id']} could not be sent to {platform}: {exc}")
                continue
            sent[key(it, platform)] = {"status": detail, "at": stamp}
            print(f"{it['id']} {platform}: {detail}")
            notify(message(it, platform))
    else:
        print("nothing due")

    for note in collect_stats(args.state.parent / "analytics", now, tokens):
        print(note)

    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(trim_state(sent, now), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
