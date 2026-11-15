# JWD Creator Studio 2.0

A local-first studio for making vertical (9:16) videos from footage you are entitled to use. It is a Django Ninja API with Celery workers, and a React front end. It upgrades the earlier "Framecut" Shorts generator.

The studio has four tabs:

| Tab | What it does |
| --- | --- |
| **Shorts Generator** | Takes an authorised YouTube URL or an uploaded video and renders up to three highlight Shorts, with captions, reframing and a QA report. |
| **AI Story Studio** | Turns a source transcript (or a YouTube URL, or your own text) into original scripts, narration, a storyboard and a rendered video made of your assets and generated graphics. |
| **Publishing Studio** | Drafts, schedules and uploads finished videos to YouTube and (with extra setup) Instagram Reels, after a rights review. |
| **Media Library** | Your uploaded and generated assets, each with an explicit licence state that controls how it can be used and published. |

Nothing here guarantees views, income or copyright clearance. The studio records what you tell it and flags what it cannot check. See [Rights and originality](#rights-and-originality) and [Known limitations](#known-limitations-and-unverified-items).

## Contents

1. [Quick start (macOS)](#quick-start-macos)
2. [System prerequisites](#system-prerequisites)
3. [AI providers](#ai-providers)
4. [Shorts Generator](#shorts-generator)
5. [AI Story Studio](#ai-story-studio)
6. [Media Library](#media-library)
7. [Publishing Studio](#publishing-studio)
8. [Rights and originality](#rights-and-originality)
9. [API overview](#api-overview)
10. [Testing](#testing)
11. [Retention and cleanup](#retention-and-cleanup)
12. [Docker Compose](#docker-compose)
13. [Known limitations and unverified items](#known-limitations-and-unverified-items)

## Quick start (macOS)

Requires Python 3.13, Node 22 or newer, Redis and FFmpeg (see [System prerequisites](#system-prerequisites)).

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
npm --prefix frontend ci
cp .env.example .env          # then edit; secrets stay in .env, which is not committed
.venv/bin/python backend/manage.py migrate
```

Django and Celery load `.env` from the project root. Non-empty values already set in the terminal take precedence. Use `DJANGO_DEBUG=true` only on a local machine; with debug off a real `DJANGO_SECRET_KEY` is required.

Run each of these in its own terminal:

```sh
# 1. Redis
redis-server --bind 127.0.0.1

# 2. API
.venv/bin/python backend/manage.py runserver 127.0.0.1:8000

# 3. Worker (default setup: one worker, default queue)
cd backend && ../.venv/bin/celery -A backend worker --pool=solo --concurrency=1 --prefetch-multiplier=1 --loglevel=INFO

# 4. Beat (exactly one process; scheduled publishing, status polling, token refresh, daily cleanup)
cd backend && ../.venv/bin/celery -A backend beat --loglevel=INFO

# 5. Front end
npm --prefix frontend run dev
```

Open the URL Vite prints (normally `http://localhost:5173`). Vite proxies `/api` to Django so cookies work on one origin. Use the same host name (`localhost` or `127.0.0.1`) throughout. For another port, add its origin to `CORS_ALLOWED_ORIGINS` and restart Django.

### Opt-in queue split

By default every task uses the `celery` queue and one worker is enough. A long render then blocks AI and publishing tasks. To avoid that, set `CELERY_SEPARATE_QUEUES=true` in `.env` and run two workers instead of the one above:

```sh
cd backend
# FFmpeg renders (generate_job, rerender_short, story render)
../.venv/bin/celery -A backend worker -Q media -n media@%h --pool=solo --concurrency=1 --prefetch-multiplier=1 --loglevel=INFO
# AI calls, publishing and anything on the default queue
../.venv/bin/celery -A backend worker -Q ai,publish,celery -n ai@%h --concurrency=2 --prefetch-multiplier=1 --loglevel=INFO
```

Do not set the flag without both workers: routed tasks would wait in queues nobody reads.

SQLite runs in WAL mode (`SQLITE_JOURNAL_MODE=wal`). Use `delete` on network filesystems, or PostgreSQL via `DATABASE_URL` (install `psycopg[binary]`) for heavier use.

## System prerequisites

| Install | What it unlocks | Without it |
| --- | --- | --- |
| `brew install ffmpeg-full` | FFmpeg with libx264 and **libass**: ASS captions with animation. Apple Silicon's `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` is auto-detected; otherwise set `FFMPEG_BIN` and `FFPROBE_BIN`. | The studio still renders. `CAPTION_RENDERER=auto` falls back to PNG-overlay captions. `CAPTION_RENDERER=ass` fails with `libass_required`. |
| `brew install fribidi` | Complex-script shaping (raqm) for Pillow. **Required for Hindi/Devanagari cards and PNG captions.** | Latin-script text works. Devanagari text is not shaped correctly, so do not use Hindi until it is installed. |
| `brew install whisper-cpp` plus a model | Local, offline transcription (`whisper-cli`). | See below. |
| Redis | The Celery broker. | Jobs cannot be queued; the API reports `queue_unavailable`. |
| Node 22+ | Front end, and public-YouTube extraction by yt-dlp (with the pinned `yt-dlp-ejs`). | No UI; URL downloads can fail (upload instead). |

**Local transcription.** After `brew install whisper-cpp`, put a multilingual ggml model at `backend/.local/models/ggml-base-q5_1.bin` (for example from the [whisper.cpp model repository](https://huggingface.co/ggerganov/whisper.cpp); the checksum for that file is SHA-1 `a3733eda680ef76256db5fc5dd9de8629e62c5e7`). Use `LOCAL_WHISPER_BIN` and `LOCAL_WHISPER_MODEL` for other paths. **Without whisper-cpp and without a Groq key, Shorts that require captions fail with a clear error** instead of producing uncaptioned video. Turn `require_captions` off only for footage with no speech.

## AI providers

Every hosted call goes through a provider layer that applies the spending policy, records each call in a usage ledger, and starts a cooldown after a 429. `GET /api/v1/providers` shows each provider, whether it is usable, and why not. Nothing is fabricated: with no usable provider the relevant step reports the reason.

| Provider | Used for | Billing class | On by default? | Env vars |
| --- | --- | --- | --- | --- |
| Local Whisper | Speech to text | Local | Yes, once installed | `LOCAL_WHISPER_BIN`, `LOCAL_WHISPER_MODEL` |
| Groq | Text, speech to text, vision | Free tier (account-dependent) | Only when `GROQ_API_KEY` is set (then allowed unless `AI_ALLOW_FREE_TIER=false` or `GROQ_ENABLED=false`) | `GROQ_API_KEY`, `GROQ_ENABLED`, `GROQ_TEXT_MODEL`, `GROQ_SPEECH_MODEL`, `GROQ_VISION_MODEL` |
| Gemini | Text, vision | Free tier | Only when key and a model id are set | `GEMINI_API_KEY`, `GEMINI_ENABLED`, `GEMINI_TEXT_MODEL`, `GEMINI_VISION_MODEL` |
| Gemini TTS | Narration | Free tier | Only when key and model are set | `GEMINI_API_KEY`, `GEMINI_TTS_MODEL` |
| Groq TTS (Orpheus) | English narration | Free tier | Only when key and model are set | `GROQ_API_KEY`, `GROQ_TTS_MODEL` |
| Hugging Face | Text, speech to text, translation | Metered | **No**; needs `AI_ALLOW_PAID_PROVIDERS=true` | `HF_TOKEN`, `HF_PROVIDER`, `HF_TEXT_MODEL`, `HF_TRANSCRIPTION_MODEL`, `HF_TRANSLATION_MODEL` |
| ElevenLabs | Narration | Paid | **No**; needs `AI_ALLOW_PAID_PROVIDERS=true` and `AI_DAILY_BUDGET_USD` above 0 | `ELEVENLABS_API_KEY`, `ELEVENLABS_MODEL`, `ELEVENLABS_VOICES`, `ELEVENLABS_PLAN_COMMERCIAL` |
| Manual narration | Uploading your own audio | Local | Always | none |

Default provider order per capability: speech to text `local_whisper, groq, huggingface`; text `groq, gemini, huggingface`; vision `groq, gemini`; translation `huggingface`; narration `manual`. Override with `AI_PROVIDER_ORDER_ASR|TEXT|VISION|TRANSLATION|TTS`.

Caveats recorded with each provider. Limits and terms change, so **check the limits, pricing, regions and data terms in your own provider console before relying on any of them**:

- **Local Whisper:** runs on your machine; accuracy depends on the model file you install. Language is auto-detected.
- **Groq:** free tier is account-dependent and a paid plan bills. Whether the free tier requires a card is unverified. Strict JSON-schema output works only on some models; others fall back to plain JSON mode. Whisper uploads are capped at 25 MB on the free tier. Vision uses a preview model with at most three images per request. Groq TTS is preview, English only, uses 200-character requests, and its commercial-use terms for generated audio were not verified (assets are marked `unknown`).
- **Gemini:** per-model free limits are unverified, and a key on a billing-enabled project is billed. On the free tier prompts and responses may be used to improve Google products and read by human reviewers, so do not send private material, and the free tier is not permitted for serving end users in the EEA, the UK or Switzerland. Model ids have no default; choose current ones from Google's docs. Hindi is listed for Gemini TTS but Hinglish quality is unverified.
- **Hugging Face:** free accounts have no included inference credits and routed providers may bill, hence opt-in. It has no text-to-speech routing here. Model ids have no defaults because catalogue availability varies.
- **ElevenLabs:** paid and opt-in. The free ElevenLabs plan has no commercial licence; generated audio is labelled restricted for monetised use unless you set `ELEVENLABS_PLAN_COMMERCIAL=true` for a plan that includes one. Hindi/Hinglish quality is unverified, and the cost figure is a conservative flat per-character estimate.

### Zero-paid policy

Paid or metered inference stays off unless you opt in:

- `AI_ALLOW_PAID_PROVIDERS=false` (legacy name `AI_ALLOW_BILLABLE`, still honoured). Either set to `true` allows paid and metered providers.
- `AI_ALLOW_FREE_TIER=true`. Set to `false` to use local providers only.
- `AI_DAILY_BUDGET_USD=0`. A ceiling in USD for priced calls; 0 blocks them all.
- Bounds on volume: `AI_MAX_CALLS_PER_JOB`, `AI_FREE_TIER_DAILY_CALLS`, `AI_DAILY_PAID_CALLS`, `AI_REQUEST_TIMEOUT`, `AI_MAX_RETRIES`, `AI_RATE_LIMIT_COOLDOWN_SECONDS`.
- A usage ledger (database table) records each attempt with provider, capability, status, units and estimated cost. Cooldowns and daily counts are read from it, so the API and workers agree. Blocked attempts are recorded but are not counted as inference calls.

The dollar figures are estimates. A spending cap on the provider side is still sensible.

## Shorts Generator

Input: a public YouTube video or Shorts URL (no playlists), or an uploaded file. You must tick the rights declaration; an optional permission reference is stored. The job returns `202` at once, a worker does the work, and the UI polls progress. Output is up to three non-overlapping 9:16 MP4s (720x1280 by default, 1080x1920 if the source is tall enough), each with a thumbnail, SRT/ASS captions, metadata and a QA report. Every finished job is `needs_review`.

Highlight choice uses transparent heuristics: scene changes, silence, speech density, transcript signals and, where available, the most-replayed signal. That signal is **unofficial** (the "heatmap" in yt-dlp's metadata, not a documented YouTube API) and is absent for many videos; its weight is `REPLAY_WEIGHT`. `HIGHLIGHT_WEIGHTS` overrides all eight signal weights.

Options (all optional; defaults reproduce the plain legacy render):

- **Presets:** `gaming_hype`, `cinematic_gaming`, `viral_commentary`, `minimal_premium`, `split_screen_gaming`, or `none`. A preset sets sensible defaults for the rest.
- **Composition:** `auto`, `blur_fill` (full frame over a blurred background), `center`, `dynamic_center`, `subject_follow`, `letterbox`, `split_screen`, `pip`. Reframing is **motion-based, not face detection**: `dynamic_center` and `subject_follow` follow where the picture is changing, which can be a menu or effect rather than a person. If there is nothing reliable to follow it falls back to a static crop. Use `blur_fill` to keep the whole HUD.
- **Caption presets:** `auto`, `bold_gaming`, `cinematic`, `dynamic_creator`, `clean_commentary`, `bilingual`; older styles `gaming`, `minimal`, `cinematic`, `dynamic` still work. Subtitle mode: original, English, Hindi or bilingual. Word-level highlighting needs word timestamps, which most sources do not have; otherwise phrases are shown whole.
- **Hook overlay:** a short on-screen opening line built from the transcript.
- **Dead-air removal:** cuts long silences inside the chosen clip.
- **Punch-ins:** gentle zoom moves on emphasised moments.
- **Split screen and secondary assets:** `split_screen` and `pip` take a secondary video or image from your Media Library (`secondary_asset_id`). A `restricted` asset is refused; `unknown` or `permission_pending` assets are accepted but force rights review on the output. `facecam_corner` places a facecam. The secondary asset never changes the source's rights status.
- **Other:** `target_duration` 20 to 60 seconds, `resolution`, `layout`, `content_category`, and `mode` (`source_highlight`, or `original_analysis` which needs an approved hosted text provider).

QA checks dimensions, duration, audio, decode errors, black or frozen sections and loudness peaks, and writes flags such as "Static or frozen section detected". The score is a technical heuristic. It does not predict audience response. Caption accuracy and audio/video sync are not verified by the machine.

## AI Story Studio

Makes original videos *about* a source (not cuts of it). Everything is a step you trigger and can review. Each step is a background job (`GET /api/v1/jobs/{id}` to poll); only one story step per user runs at a time.

1. **Create a project.** Give a YouTube URL (with a rights declaration), or paste a transcript. Choose language (`en`, `hi`, `hinglish`), content format (for example gaming news, gameplay analysis, myth vs fact), narration style, target length (20-30, 30-45 or 45-60 seconds) and 1 to 3 videos.
2. **Analyse.** The source is split into sections, repeated information is collapsed, and claims are extracted. A text provider helps if one is usable; otherwise a deterministic extractor is used. Each claim has a status:
   - `source_claim`: the source states it and a supporting quote was found verbatim in the text.
   - `inferred`: a model inference, or a claim whose quote could not be found (downgraded).
   - `verified`: you attached evidence (a URL or note) yourself. The studio does not fact-check.
3. **Concepts.** The writer proposes up to six distinct angles; you select up to your video count.
4. **Scripts.** An original script is written per concept, with claim markers. If no text provider is usable you get a clear `text_provider_unavailable` error listing reasons. **Manual path:** `POST /story/projects/{id}/scripts/manual` accepts your own script, which skips the writer entirely.
5. **Quality score and regeneration.** Each script is scored 0 to 1 on structure, length fit, grounding (claims marked and numbers not invented), originality against the source, hook and readability. The pass mark is 0.70 and all hard gates must pass. **This is an editorial heuristic, not a prediction of views.** Regeneration is bounded: up to 3 attempts per round and 6 per script, and the feedback from the previous score is passed to the next attempt.
6. **Approve** the script, which fixes the text.
7. **Narration.** Choose one of: generate with a TTS provider (Gemini TTS, Groq TTS, ElevenLabs, subject to the policy above), upload your own recording, or attach an existing narration asset. Voice previews are cached for a day. Word timing is **approximate** (interpolated from the audio length), not forced alignment. Generated narration is marked synthetic: the YouTube draft sets `altered_content`, and the upload sends YouTube's `status.containsSyntheticMedia`.
8. **Storyboard.** One scene per script section, timed from the narration. Visuals follow a rights hierarchy: your `owned` assets first, then `commercial_use_permitted`, then `attribution_required` (attribution is recorded and added to the post), then original motion-graphic cards (fact, comparison, callout, numbered list, title), which always work. Third-party `source_clip` scenes need a rights declaration, must be tied to a claim, and are capped at 35% of the runtime. You can edit scenes and swap assets.
9. **Render.** Needs a usable FFmpeg; blocked if the storyboard uses a `restricted` asset. Outputs appear as story videos with a QA report and a rights status.

### Rights gating in story videos

A story video made only from your own work and generated graphics still starts at `manual_review_required`. Third-party footage, unknown or pending licences, or generated narration of unverified licensing force review. Publishing requires a documented rights review (see [Publishing Studio](#publishing-studio)). A story video built from a pasted transcript has no source video, so it can never be published on the "source declaration" basis; only a completed rights review counts.

## Media Library

Upload video, audio, music, sound effects, images and graphics (size and count limits apply; image dimensions are bounded; files are checked by content, not extension). Generated narration, graphics and ingested source clips also appear here with their origin. Assets are private to your session, soft-deleted first and purged later, and cannot be removed while a storyboard uses them.

Each asset has one of six licence states:

| State | Meaning | Effect |
| --- | --- | --- |
| `owned` | You made it or hold all rights. | Usable. |
| `commercial_use_permitted` | A licence or permission allows commercial use. | Usable. |
| `attribution_required` | Usable if credited. | Usable; attribution text is required and is added to posts. |
| `permission_pending` | You have asked but not been told yes. | Usable for drafts; forces manual rights review. |
| `unknown` | Default. You have not established the terms. | Usable for drafts; forces manual rights review. |
| `restricted` | You may not use it commercially or at all. | Blocks story renders and publishing. |

The licence is what *you* record, plus an optional reference and notes. The studio cannot verify it.

## Publishing Studio

Drafts, scheduling (timezone, preferred slots, suggested times), approval, queue, failures and connected accounts. A post can only be approved after its video has a completed rights review (`POST /shorts/{id}/rights-review` with the permission and music confirmations), or, for Shorts, the one-click "Schedule on YouTube" flow, which records `source_declaration` as the basis (your declaration, not a check). Assets with blocking licences, or needing attribution without text, block approval.

### YouTube

YouTube uses Google's [official OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server) and the resumable upload API. Tokens and upload-session URIs are Fernet-encrypted. Future-dated posts upload as private with a native `publishAt`; immediate posts request public. If YouTube keeps a video private the post is marked `uploaded_private` rather than "published". A five-minute beat task confirms when a scheduled video goes public.

1. In [Google Cloud](https://console.cloud.google.com/), create or select a project and enable **YouTube Data API v3**. In Google Auth Platform, configure Branding and an **External** Audience for a personal Google account. While the app is in Testing, add your Google account as a test user. In Data Access add `youtube.upload` and `youtube.readonly`.
2. In Google Auth Platform, Clients, create an OAuth client of type **Web application**. Add this exact **Authorized redirect URI**: `http://localhost:8000/api/v1/social/youtube/callback`. Use `localhost` for the front end too (`http://localhost:5173` or `http://localhost:5174`). If you use `127.0.0.1` in the browser, use it consistently in the redirect URI and `.env`.
3. Generate a Fernet key with `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. In `.env` set `SOCIAL_ENCRYPTION_KEY` to it, set `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` from the new client, and set `YOUTUBE_REDIRECT_URI` to the exact registered URI. Set `SOCIAL_FRONTEND_URL` to the front-end address you use. Do not commit `.env` or change the key after connecting; losing it makes stored accounts unrecoverable.
4. Restart Django and the worker after editing `.env`. Keep Redis, the worker and beat running. In the studio open **Publishing, Connect YouTube**, sign in with the added test user, choose the channel and approve the two scopes.

Google's [Testing audience](https://support.google.com/cloud/answer/15549945) grants expire after seven days, so a test connection may need reconnecting. Production OAuth verification is separate. Per the [YouTube Data API revision history](https://developers.google.com/youtube/data/revision_history), from 2 November 2026 uploads from unverified API projects are no longer automatically forced private; account settings and other policy checks can still affect the result. Upload quota applies.

### Instagram

Instagram publishing uses the official Instagram API with Instagram Login. It is **off until you set it up**, and it needs more than YouTube:

- A **Meta developer app** with Instagram product, `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` and a registered `INSTAGRAM_REDIRECT_URI`.
- An Instagram **professional** (business or creator) account, connected from the studio.
- Meta **app review** for the publishing permission before accounts other than your own test accounts can use it. Check Meta's current requirements.
- **`PUBLIC_MEDIA_BASE_URL`**: a public `https` address that serves `MEDIA_ROOT/public_reels` (and nothing else). Meta's servers fetch the video from a public URL; they cannot reach `localhost`. On a pure localhost setup, Instagram publishing is unavailable (`GET /api/v1/capabilities` then reports `instagram_publishing_available: false`).
- Our own scheduler: Instagram has no native scheduling, so approved posts are published by a beat task (checked every minute) once `scheduled_at` passes. **The worker and beat must both be running at that time**, which means the computer must be on.

`GET /api/v1/social/platforms` shows, per platform, whether it is configured, connected and able to publish, with the reason when not.

## Rights and originality

What the app records:

- Your rights declaration and permission reference for each source (a declaration, not verified ownership).
- Licence state, reference, notes and attribution text for each asset.
- For each output, a rights block and a `rights_review_status` (`manual_review_required`, `rights_documented`, and so on), plus which assets and source clips it uses.
- Whether narration was synthetic (disclosure flag for YouTube).

What it cannot determine:

- Whether you truly own or may use the source, music or footage.
- Whether captions, crops, commentary or a rewrite make reuse permissible, or whether YouTube or Instagram will treat the video as original, monetisable or eligible for reuse.
- Whether audio contains music or third-party material.

AI rewriting is **not** copyright clearance. The originality check compares a script with the source transcript using word-sequence overlap and flags long copied runs; it is a heuristic, does not detect translation or cross-script copying, and says nothing about the images or audio. Review every output yourself before publishing.

## API overview

Interactive OpenAPI docs: `http://localhost:8000/api/docs`. Start with `GET /api/v1/session`, which creates the private browser session and returns `csrf_token`; send cookies and `X-CSRFToken` on mutating requests. Errors use `{error, message}`; validation errors (422) also keep the `detail` list. All routes are under `/api`.

| Group | Main routes |
| --- | --- |
| System | `GET /v1/session`, `/v1/health`, `/v1/capabilities`, `/v1/providers` |
| Shorts jobs | `POST /v1/jobs`, `POST /v1/uploads`, `GET /v1/jobs`, `/v1/jobs/{id}`, `/results`, `/analysis`, `POST /v1/jobs/{id}/cancel`, `/retry`, `/regenerate`, `GET /v1/jobs/{id}/download` (ZIP) |
| Shorts and files | `GET /v1/shorts/{id}`, `/video`, `/thumbnail`, `/download`, `/captions?format=srt|ass`; metadata and transcript editing; `POST /v1/shorts/{id}/rights-review` |
| Story | `/v1/story/projects` (create, update, delete), `.../analyze`, `.../claims/{id}`, `.../concepts/generate|select`, `.../scripts/generate|manual`, `/v1/story/scripts/{id}` (edit, approve, regenerate, narrate, narration, storyboard), `/v1/story/scenes/{id}`, `.../render` |
| Narration | `GET /v1/narration/voices`, `POST /v1/narration/preview|synthesize|upload` |
| Media Library | `/v1/assets` (list, upload), `/v1/assets/{id}` (get, patch, delete, `/file`, `/thumbnail`) |
| Publishing | `/v1/social/accounts`, `/platforms`, `/posts` and `/posts/{id}` (`approve`, `cancel`, `metadata`), `/approve-batch`, `/preferences`, `/suggestions`, YouTube and Instagram `connect`/`callback`, `POST /v1/jobs/{id}/schedule-batch` |

`POST /api/generate-short` remains as a deprecated alias for `POST /v1/jobs`.

## Testing

```sh
DJANGO_DEBUG=true .venv/bin/python backend/manage.py test api
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

All provider, Meta and YouTube behaviour is tested with mocks; no network is used. Some tests skip rather than fail when the environment lacks something:

- `ffmpeg required`: the machine has no FFmpeg, so real encode tests are skipped.
- `libass`: FFmpeg lacks libass, so the ASS caption burn-in tests are skipped.
- `raqm (fribidi) not available`: Hindi/Devanagari shaping tests are skipped until `fribidi` is installed.
- Two tests skip when your local `.env` sets `CELERY_SEPARATE_QUEUES` or AI policy variables, because they assert the defaults.

With the API, Redis and a worker running, `scripts/smoke.py` exercises the whole asynchronous HTTP path with a synthetic test pattern (no real footage). Run it manually.

## Retention and cleanup

```sh
.venv/bin/python backend/manage.py cleanup_studio
docker compose exec api python manage.py cleanup_studio    # Docker
```

`cleanup_studio` marks abandoned jobs failed, deletes source footage after `SOURCE_RETENTION_DAYS` (default 1), deletes terminal jobs and their folders (Shorts and story jobs, including story jobs without a source video) after `OUTPUT_RETENTION_DAYS` (default 7) unless a draft or queued post still needs them, purges assets past their retention date (this includes cached narration previews, kept one day) and soft-deleted assets after seven days, and removes empty projects. Assets pinned or used in a storyboard are kept.

**Schedule it.** Either run it hourly from cron or launchd, or rely on the daily Celery beat task `api.tasks.cleanup_studio_task` (which calls the same command) as long as beat is running. If neither runs, nothing is deleted and a crashed worker can leave a job "in progress" indefinitely.

## Docker Compose

```sh
cp .env.example .env     # set a unique secret; local HTTP needs DJANGO_DEBUG=true
docker compose up --build
```

Open `http://localhost:8080`. Compose starts Redis, a migration service, the API, a `worker` on the `media` queue, a `worker-ai` on `ai,publish,celery`, `beat` and the web container. It sets `CELERY_SEPARATE_QUEUES=true` unless you override it. The configuration is validated, but **it has not been built or run here** (the Docker CLI was unavailable), so treat the first build as untested. It uses SQLite on a named volume for a single-host setup.

For anything exposed to the internet: HTTPS and real authentication in front (anonymous browser sessions are not an account system), `DJANGO_DEBUG=false`, a unique secret, exact hosts and origins, volume-level disk limits and backups. Do not expose Redis, the data volume or Django admin.

## Known limitations and unverified items

- **No live call** to any hosted LLM, TTS, vision, Meta or YouTube API has been made in this upgrade. All of that behaviour is tested with mocks. Review a test upload in a controlled channel first.
- **YouTube synthetic-media flag:** `status.containsSyntheticMedia` is documented in the YouTube Data API reference and is sent when a post is marked altered; the live effect has not been observed.
- **Hindi rendering** is untested locally until `fribidi` (raqm) and libass are installed; those tests skip. Hinglish and Hindi TTS quality are unverified.
- **Subject-follow** and dynamic crops are heuristic and motion-based; they are not face or object detection and can follow the wrong thing.
- **Word-level caption highlighting** is rarely available because most transcripts only have phrase timing. **Narration word timing** is approximate.
- **Most-replayed signal** is unofficial and absent for many videos.
- **Story videos made mostly of static cards** can get a "Static or frozen section" QA flag; it is a prompt to review, not necessarily an error.
- **Originality check** is word-overlap only: it does not detect translation or cross-script copying, and does not look at visuals or audio.
- **Quality scores** are editorial heuristics, not predictions.
- **Instagram** is unusable on a pure localhost setup (needs `PUBLIC_MEDIA_BASE_URL`, a Meta app, app review and a professional account), and needs worker plus beat running at the scheduled time.
- **Provider limits and terms** (free-tier quotas, card requirements, regions, commercial use of generated audio) change and several are recorded as unverified; check them yourself.
- **Docker** configuration is not built or run here.
- **Rights:** declarations are not verified, and music and third-party content are not detected.
- Cancellation takes effect at the next stage boundary, not mid-FFmpeg. The studio is single-user-per-browser-session, not a multi-user system.
- Not implemented: object storage, signed URLs, cross-worker currency budgets beyond the ledger, production error monitoring.

`description.md` is the original prototype specification and no longer describes the product.
