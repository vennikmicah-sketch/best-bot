# Deploying on Railway

## 1. Push to GitHub
Put these files in a repo: `bot.py`, `requirements.txt`, `railway.json`, `Procfile`, `.gitignore`.
Do NOT commit `.env` (it holds your token).

## 2. Create the service
1. railway.com → **New Project** → **Deploy from GitHub repo** → pick your repo.
2. Railway detects Python from `requirements.txt` and runs `python bot.py`.
3. This is a worker, not a web server, so you don't need to generate a domain.

## 3. Add a Volume (important!)
Without a volume, Railway wipes the filesystem on every redeploy and you lose all stock, files and drop schedules.

1. In your service: **Settings → Volumes → Add Volume** (or right-click the service on the canvas → **Attach Volume**).
2. Set the mount path to `/data`.

## 4. Variables
In the service **Variables** tab add:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | your bot token |
| `DATA_DIR` | `/data` (must match the volume mount path) |
| `GUILD_ID` | optional, your server ID for instant slash-command sync |

## 5. Deploy
Railway redeploys when you save variables. Check the **Deployments → Logs** tab for `Logged in as ...`.

## Notes
- Keep it to **1 replica**. Two copies would double-drop.
- Railway's free trial credits run out; a worker like this uses very little, but it needs an active plan to stay online 24/7.
- Slash commands can take up to an hour to appear globally. Setting `GUILD_ID` makes them show up instantly.
- To back up stock, download `/data/bot.db` using the Railway CLI (`railway ssh`) or keep your stock lists in .txt files so you can re-run `/stock bulk`.
