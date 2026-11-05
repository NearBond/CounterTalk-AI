# Installing CounterTalk

This guide covers running CounterTalk on your own machine, loading the demo data, and deploying it. What CounterTalk is: [README.md](README.md).

## What you need

- **Docker,** for Postgres 17 (`compose.yaml`).
- **Python 3.12** and **[uv](https://docs.astral.sh/uv/).**
- **Node.js 22.12 or later** and **npm.** Linux distributions often package an older one; [nvm](https://github.com/nvm-sh/nvm) installs a newer one.
- **ffmpeg and ffprobe** on your PATH: the worker converts and splits recordings with them. For example `winget install Gyan.FFmpeg` on Windows, `brew install ffmpeg` on macOS, or `sudo apt install ffmpeg` on Debian and Ubuntu.
- **A Meta API key,** to process recordings. Optional: without one, everything else works ([below](#without-a-meta-api-key)).

## 1. Start Postgres

From the repo's root:

```sh
docker compose up -d db
```

This starts Postgres on 127.0.0.1:5432, with user, password and database `countertalk`. The tests make their own `countertalk_test` database there. If something else already uses port 5432, stop it, or change the port in `compose.yaml` and in `DATABASE_URL`.

## 2. Settings

```sh
cd backend
cp .env.example .env
```

In Windows cmd, use `copy .env.example .env`. Then fill in `backend/.env`. It's git-ignored: never commit it.

| Setting | Needed? | What it does |
|---|---|---|
| `SECRET_KEY` | **Yes** | Signs sign-in sessions. The API won't start without it unless `DEBUG=1`. Make one with `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `MODEL_API_KEY` | To process recordings | One Meta key for both Muse Voice Transcribe and Muse Spark. |
| `DATABASE_URL` | Already set | The compose database. `postgres://` and `postgresql://` URLs both work. |
| `DATA_DIR` | Already set | Where audio, chunks and transcription replies are kept. A relative path is inside `backend/`. |
| `WEB_DIST_DIR` | Already set | The built web app, which the API serves. |
| `DEBUG` | Leave at `0` | `1` turns on development-only extras. |
| `DEMO_SIGNIN_PASSWORD` | Optional | Set it to `metahackathon` with the demo data, and the sign-in page lists the demo accounts. Empty, it lists none. |
| `AUDIO_RETENTION_DAYS` | Already set | Audio is deleted this many days after its visit is confirmed (30 if unset). The example sets `36500`, so the demo data's audio stays: its visits were confirmed in late October and early November 2026, and with 30 the worker would delete their audio once they're older than that. |


## 3. Install and migrate

Still in `backend/`:

```sh
uv sync
uv run alembic upgrade head
uv run python scripts/check_setup.py
```

`check_setup.py` checks Python, uv, ffmpeg, ffprobe, the settings, the database and the keys, one line each: `ok`, `skip` or `fail`. A `skip` for an optional key that only dev tools use is fine. An empty `SECRET_KEY` shows as `skip` too, but the API won't start without it.

## 4. Load the demo data

On the freshly migrated, empty database, from `backend/`:

```sh
uv run python scripts/load_demo_data.py
```

- **What it loads:** the PepsiCo India example org (`demo/pepsico-india.sql`), with its shops, people, visits, issues and summaries, and the visits' recordings (`demo/visits/<visit_id>/playback.m4a`), copied into `DATA_DIR`. It needs no Meta key.
- **It refuses** to run on a database that isn't migrated or already has an org.
- **The database user must be a superuser,** because the load turns triggers off while it runs. The compose database's user is one.
- **It prints the sign-ins:** the eight example.com accounts listed in [README.md](README.md#try-the-demo), all with the password `metahackathon`.

To start without the demo data instead, `uv run python scripts/seed_dev.py` creates the PepsiCo India org on its pack, with its shops, three reps and one example visit whose problem is already fixed. Then give a user a password, for example `NEW_PASSWORD=<password> uv run python scripts/set_password.py rep@example.com`, or in PowerShell `$env:NEW_PASSWORD="<password>"; uv run python scripts/set_password.py rep@example.com`. For another company's pack, edit `seed_dev.py`'s constants first (see [README.md](README.md#running-it-for-your-own-company-what-you-need)).

## 5. Build the web app and run

Build the web app once, from the repo's root:

```sh
cd web
npm ci
npm run build
```

Then run the API and the worker, each in its own terminal, from `backend/`:

```sh
uv run uvicorn countertalk.main:app --host 127.0.0.1 --port 8000
uv run python -m countertalk.worker
```

Open http://127.0.0.1:8000. Signed out, it shows the landing page; **Try the demo** goes to the sign-in page, http://127.0.0.1:8000/signin. Use Chrome, Edge or Firefox for the local app.

- **The API** serves the web app from `web/dist`, and the API itself under `/api`.
- **The worker** runs the background jobs: transcription, extraction, summaries and the daily retention. Without it, nothing new is processed.
- **To work on the web app,** run `npm run dev` in `web/` as well, and open http://127.0.0.1:5173. It sends `/api` to the API on port 8000.

## Without a Meta API key

Leave `MODEL_API_KEY` empty, and a warning bar on every signed-in page says so.

- **Everything else works:**
  - browsing the visits, with their transcripts, findings and ▶ quotes;
  - reviewing and confirming a visit that's already processed;
  - the inboxes and issues, and resolving them;
  - the Updates and Fixed since the last visit lists;
  - reading the summaries and briefs already written;
  - the catalog.
- **These need the key:**
  - processing new recordings (transcription, speaker roles and extraction);
  - Ask;
  - generating a brief;
  - writing a summary's words. A summary with nothing to cite is still written, with its numbers only; one that needs words fails until the key is set, and **Try again** works after that.
- **A recording made without the key** fails after a few minutes of retries. Once the key is set and the worker restarted, process it again with `uv run python scripts/reprocess_failed.py --code stt_not_configured --since <YYYY-MM-DD>` (it lists them), then the same with `--yes`. A visit that was already transcribed waits, and is extracted on its own once the key is there.

## Tests

From `backend/` (the compose Postgres must be running):

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

From `web/`:

```sh
npm run lint
npm test
```

`uv run pytest` never calls Meta. The live checks, which call Meta and cost money, run only with `uv run pytest -m live`, and skip without a key.

On macOS, the `start.sh` tests need bash 4.3 or later (`brew install bash`): the system bash is 3.2.

## Deploying

The root [`Dockerfile`](Dockerfile) builds one image: the web app (Node 22), then the backend (Python 3.12), with ffmpeg. Its `.dockerignore` lets only `backend/` and `web/` source into it. On start, [`backend/scripts/start.sh`](backend/scripts/start.sh) checks the database, runs the migrations, then starts the API on `$PORT` and the worker.

On your host:
- **Postgres 17 or later,** and a **persistent volume** mounted at `/data`.
- **Variables:** `DATABASE_URL`, `DATA_DIR=/data`, `DEBUG=0`, a new `SECRET_KEY` and `MODEL_API_KEY`; with the demo data, `AUDIO_RETENTION_DAYS=36500` too. Optionally `DEMO_SIGNIN_PASSWORD`. Keep the keys in the host's secret settings, never in the repo.
- **Health check:** `/api/health`, which answers `{"status": "ok", "database": "ok"}`. Restart on failure.

**On Railway:** add a Postgres database and a service built from the `Dockerfile`, give the service a volume at `/data` and the variables above, and deploy with `railway up` from a clean checkout. Railway sets `PORT`, and the image sets `WEB_DIST_DIR`.

The image doesn't contain `demo/`. To show the demo data on a host: deploy once, so the migrations run on the empty database; then copy `demo/` onto the volume and run the loader inside the container, which looks for it at `/app/demo`. On Railway, from the repo's root:

```sh
railway volume files --volume <volume> upload demo /demo --concurrency 2
railway ssh -s <service>
```

Then, in that shell: `ln -sfn /data/demo/demo /app/demo && python scripts/load_demo_data.py`. It copies the recordings into `/data/visits`. Afterwards, delete the uploaded copy with `rm -rf /data/demo`.
