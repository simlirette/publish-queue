# publish-queue

Sends approved posts to the TikTok inbox on schedule, without the laptop being on.

```
laptop (media-agent)                       GitHub (this repo)                         TikTok
approve in review app  --push gh-pages-->  Pages serves q/<id>/*.jpg + item.json
                                           Action every 30 min: slot reached? --->   inbox notification
                                           state/sent.json records what went out     you pick a sound, post
```

- **main** holds this README, `publish.py` and the workflow. **gh-pages** holds the queue; the laptop
  force-pushes it as a single commit each time, so old slides never accumulate.
- Mode is TikTok `MEDIA_UPLOAD` (inbox): no TikTok audit needed. Direct Post would need one.
- Limits: TikTok allows at most 5 pending inbox uploads per 24 h; a slot missed by more than 12 h
  is skipped (and you get a Telegram message); GitHub may start scheduled runs a few minutes late.

## One-time setup (about 30 minutes)

### 1. GitHub repo + Pages
1. On github.com create a **public** empty repo `publish-queue` (Pages needs public on the free plan).
2. In PowerShell:
   ```powershell
   cd $HOME
   git clone https://github.com/simlirette/publish-queue.git
   Copy-Item media-agent\deploy\publish-queue\publish.py, media-agent\deploy\publish-queue\README.md publish-queue\
   Copy-Item -Recurse media-agent\deploy\publish-queue\.github, media-agent\deploy\publish-queue\state publish-queue\
   cd publish-queue; git add -A; git commit -m "Publishing workflow"; git push
   ```
3. In media-agent, `config/app.yaml` > `publish`: `repo_url` and `pages_url` are already filled in.
   Run `uv run carousel queue init` (creates `data/publish-site/` and pushes gh-pages).
4. Repo > Settings > Pages > Build and deployment: **Deploy from a branch**, `gh-pages`, `/ (root)`.
   After a minute, https://simlirette.github.io/publish-queue/callback.html must open.

### 2. TikTok developer app (developers.tiktok.com)
1. Create an app (platform **Web**). Add products **Login Kit** and **Content Posting API**.
2. Login Kit redirect URI: `https://simlirette.github.io/publish-queue/callback.html`
3. Scopes: `user.info.basic`, `video.upload`. Direct Post is not needed.
4. Create a **Sandbox** and add the TikTok account you post to as a target user.
5. **URL properties** > verify by **URL prefix**: `https://simlirette.github.io/publish-queue/`.
   Download the signature file, put it in `media-agent\data\publish-site\`, run
   `uv run carousel queue push`, wait a minute, then click Verify.
6. Copy the client key and secret into media-agent `.env` (`TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`).

### 3. Secrets
1. Install the GitHub CLI once: `winget install GitHub.cli`, then `gh auth login`.
2. Create a fine-grained token (github.com > Settings > Developer settings) for this repo only with
   **Secrets: read and write**, then `gh secret set GH_PAT -R simlirette/publish-queue` and paste it.
   It lets the Action save TikTok's refresh token when TikTok rotates it.
3. Optional Telegram pings: `gh secret set TELEGRAM_BOT_TOKEN -R simlirette/publish-queue` and
   `TELEGRAM_CHAT_ID` (same values as in media-agent `.env`).
4. `uv run carousel tiktok auth` (in media-agent): signs in to TikTok and sets `TIKTOK_CLIENT_KEY`,
   `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN` and the `PAGES_URL` variable.

### 4. Test, then switch on
1. Set `publish.mode: queue` in media-agent `config/app.yaml`.
2. Schedule one approved post in the review app. `uv run carousel queue status` shows its id.
3. Repo > Actions > publish > **Run workflow**, item = that id. A TikTok inbox notification should arrive.
4. From then on the Action sends each post at its slot. Mark it posted in the review app as before.

## Troubleshooting
- `url_ownership_unverified`: the signature file is not at the prefix, or Pages has not deployed.
- `spam_risk_too_many_pending_share`: five uploads are waiting in the inbox; post or delete them.
  The Action retries on its next run.
- Token refresh failed: run `uv run carousel tiktok auth` again (refresh tokens last about a year).
