# effective-octo-adventure

Telegram moderation bot packaged for Docker deployment on a VPS.

## Environment

Copy `.env.example` to `.env` and fill in:

- `BOT_TOKEN`
- `CREATOR_ID`
- `ALLOWED_CHAT`
- `IMAGE_NAME`

Optional:

- `DATA_FILE` defaults to `/app/data/db.json`
- `WATCHTOWER_POLL_INTERVAL` defaults to `60` seconds

## Local run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Production deploy

1. Build publishing is handled by GitHub Actions on pushes to `main`.
2. The VPS runs `docker-compose -f docker-compose.prod.yml up -d`.
3. `watchtower` checks for new `latest` image tags and restarts the bot automatically.

Server bootstrap example:

```bash
mkdir -p /opt/iris-bot
cd /opt/iris-bot
```

Put these files on the server:

- `docker-compose.prod.yml`
- `.env`

Then start:

```bash
docker-compose -f docker-compose.prod.yml up -d
```

If the package is private in GHCR, log in once on the server:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```
