# Framecut — authorized-footage Shorts Studio

An incremental upgrade of the original Django Ninja / React repository. Submit an authorized YouTube URL or upload your original footage, receive a persistent job immediately, and let a bounded Celery worker produce up to three distinct 9:16 MP4s. The studio shows actual worker progress, private previews, editing/QA reports, caption downloads, recent history and a batch ZIP.

## Start locally (MacBook Air / modest CPU)

Requires Python 3.13, Node 22+, Redis, FFmpeg with libx264/libass, and `whisper-cli` for offline speech captions. The worker uses Node with the pinned `yt-dlp-ejs` package for public YouTube extraction. Local transcription uses a multilingual Whisper model on CPU; PyTorch, TensorFlow and GPU inference are not required.

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
npm --prefix frontend ci
cp .env.example .env
```

Install `whisper-cli` (on macOS, `brew install whisper-cpp`), then place the multilingual model at `backend/.local/models/ggml-base-q5_1.bin`. For example, download it from the [whisper.cpp model repository](https://huggingface.co/ggerganov/whisper.cpp) and verify SHA-1 `a3733eda680ef76256db5fc5dd9de8629e62c5e7`. This local file is ignored by Git. Set `LOCAL_WHISPER_BIN` and `LOCAL_WHISPER_MODEL` if using different paths.

Edit `.env`. Django and Celery automatically load it from the project root; nonempty values already set in the terminal take precedence. Use `DJANGO_DEBUG=true` only for local development. A production secret is mandatory with debug disabled. Run migrations once:

```sh
.venv/bin/python backend/manage.py migrate
```

Run these in separate terminals:

```sh
redis-server --bind 127.0.0.1
.venv/bin/python backend/manage.py runserver 127.0.0.1:8000
```

```sh
cd backend
../.venv/bin/celery -A backend worker --pool=solo --concurrency=1 --prefetch-multiplier=1 --loglevel=INFO
```

```sh
npm --prefix frontend run dev
```

Open the URL Vite prints, normally `http://localhost:5173`. Ports 5173 and 5174 with either `localhost` or `127.0.0.1` are trusted by the sample local configuration. Vite proxies `/api` to Django, so cookies and private previews work on the same origin. Use the same hostname consistently. If choosing a different port, add its origin to `CORS_ALLOWED_ORIGINS` and restart Django. Do not override this variable with only one port when another frontend port is in use. For a separate frontend origin, set `VITE_API_BASE=http://localhost:8000/api` and configure CORS accordingly.

On macOS, the minimal Homebrew FFmpeg build may omit libass. `brew install ffmpeg-full` supplies it; Apple Silicon's `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` is auto-detected. Alternatively set `FFMPEG_BIN` and `FFPROBE_BIN`. The Docker backend includes FFmpeg/libass, licensed DejaVu fonts, `whisper-cli`, and the verified multilingual model. Homebrew's full FFmpeg build installs additional libraries; this application does not invoke its local inference filters.

## What works

- UUID source/job/short models and migrations; Celery + Redis; immediate `202` responses, durable progress, atomic job claim, failure codes, cancellation between operations, retries and whole-job regeneration.
- Strict public HTTPS YouTube video/Shorts URL validation, no playlists or arbitrary media URLs; yt-dlp download with safe names and permission declaration. YouTube captions are not requested. Authentication/DRM bypass is not implemented. Upload is the fallback when a permitted download is unavailable.
- ffprobe actual FPS/duration/streams; file/duration/resolution bounds; bounded scene-change sampling, silence analysis and transparent candidate scores. Interval scheduling selects up to three non-overlapping moments and penalizes substantially repeated dialogue. Low semantic confidence is disclosed.
- Hosted Hugging Face speech recognition and structured transcript/editorial analysis when explicitly configured. Hosted editorial signals influence ranking of the top six heuristic candidates. Pydantic validates model responses. Remote errors fall back with visible warnings; no synthetic transcripts or fabricated AI results.
- Source audio is extracted to mono 16 kHz PCM and transcribed locally with `whisper-cli` before highlight selection. Configured hosted ASR is an optional fallback. Four distinct ASS caption styles, JSON/SRT/ASS subtitle artifacts and transcript editing with background re-rendering, high-contrast safe margins and restrained phrase animation. Whisper segment timestamps are retained; subdivisions within phrases use **approximate interpolation**, not claimed word-level alignment.
- Real CPU FFmpeg H.264/AAC, 720×1280 by default, optional 1080×1920 when source height supports it; source-compatible frame-rate conversion; full-frame blurred-background layout or explicit center crop, gentle color adjustment, fades, loudness normalization and peak limiting. Full-frame layout preserves HUD/context instead of guessing a subject.
- Real thumbnails and playback; individual MP4/SRT/ASS downloads, metadata copy/regeneration, full-job regeneration, ZIP and session-scoped history.
- QA probes dimensions, duration and audio; full decode checks corruption; black/freeze detection and peak measurements flag review items. Technical repair gets one bounded repeat. Editorial/caption accuracy, final-word completeness and perceptual A/V sync are explicitly **unverified**. The score is a technical heuristic, never an audience/virality claim.
- Rights declarations and optional references stored with source attribution. All finished jobs use `needs_review`, and Shorts require manual rights review. A declaration or reference is not verified ownership; captions/crops do not establish monetization eligibility.
- Session-scoped private assets with CSRF on cookie-authenticated mutations; no public media-directory route; URL/path traversal checks; hourly per-session and global queue caps; bounded subprocesses, disk checks and retention cleanup.

## Hosted AI setup and current availability

Hosted inference is **off by default**. Set `AI_ALLOW_BILLABLE=true` only after you explicitly approve possible charges, then set `HF_TOKEN` to a fine-grained token with Inference Providers permission. A token alone never enables calls. Set `HF_TRANSCRIPTION_MODEL`, `HF_TRANSLATION_MODEL`, `HF_TEXT_MODEL` and optionally `HF_PROVIDER` to models/providers that currently support the task. These values have no hardcoded defaults: catalog availability, region, model license and account credits vary.

Checked against official documentation on **14 November 2026**:

- The [SDK task/provider matrix](https://huggingface.co/docs/huggingface_hub/guides/inference) distinguishes ASR from chat completion; their request formats and provider coverage differ. The application uses `InferenceClient.automatic_speech_recognition` and `InferenceClient.chat_completion`, never invented endpoint URLs.
- The [ASR task page](https://huggingface.co/docs/inference-providers/tasks/automatic-speech-recognition) lists `openai/whisper-large-v3` as a recommended hosted model and documents `hf models ls --warm --pipeline-tag automatic-speech-recognition --sort trending_score` for discovery. This is a **remote** option, not a local model download or a guarantee that your account can route it.
- The [chat completion task page](https://huggingface.co/docs/inference-providers/tasks/chat-completion) lists instruction models such as `google/gemma-2-2b-it` and `openai/gpt-oss-120b`. Check live provider mappings and JSON support before configuring one. Models aren't interchangeable across tasks.
- [Current pricing](https://huggingface.co/docs/inference-providers/pricing) states free accounts have **no included monthly inference credits**; purchased credits or a subscription may be required. No unlimited free inference is assumed, and no paid account is provisioned by this project.

Without hosted inference, local Whisper supplies speech text. `require_captions` defaults to true: if no usable speech transcript is generated, the job fails clearly instead of presenting an uncaptained result. Disable this option only for footage known to be speech-free. A source with enough duration can yield three clips, but semantic coherence cannot be certified by heuristics.

`HIGHLIGHT_WEIGHTS` accepts a JSON object with all eight scoring signals; values are normalized and invalid weights are rejected.

`AI_REQUEST_TIMEOUT`, `AI_MAX_RETRIES` and `AI_MAX_CALLS_PER_JOB` bound requests. One worker makes remote calls sequentially. Logs record request attempts, timings and status without tokens. 400/401/402/403/404/422 are not retried; transient failures get exponential backoff. Malformed editorial JSON gets one additional structured-response attempt. Job reports contain inference calls, per-stage time, total time and measured job-directory bytes (not a sampled peak). A provider/account-side spending cap remains necessary: this release does not infer dollar cost or implement a cross-worker daily currency budget. Metadata regeneration uses the same adapter but is a synchronous explicitly requested action.

Hosted provider failure paths and schemas are tested with mocks. Local Whisper was exercised on a short spoken test recording; automated media tests use generated footage. A public YouTube source was downloaded during local verification, but its contents and caption accuracy still require user review.

## Bilingual captions and social queue

Automatically generated captions are marked for human accuracy review. Each timed transcript segment stores spoken text, an English translation, a Hindi translation, a language label, timestamps and available confidence. A lightweight script/word detector marks likely language changes, including Romanized Hindi, but it is not verified language detection and must be reviewed. Hosted translation is available only through a configured and explicitly enabled provider. Missing translations are omitted with a job warning; they are never fabricated. Devanagari ASS rendering requires licensed font coverage (Noto Sans Devanagari in Docker, Arial Unicode MS on this Mac). The transcript editor persists corrections and queues an FFmpeg re-render from retained source footage.

Original Analysis is an optional text commentary overlay. It requires a configured and approved text provider plus source transcript; generated commentary is validated against an exact source quote. It is text-based; licensed voice narration is still open. Its presence does not establish copyright permission or monetization eligibility.

The Schedule screen supports drafts, editable metadata, timezone/slots, suggested times, calendar, queue, failures, and connected YouTube accounts. YouTube connection uses the [official OAuth web server flow](https://developers.google.com/identity/protocols/oauth2/web-server) with offline refresh; credentials and resumable upload session URIs are Fernet-encrypted. Set `SOCIAL_ENCRYPTION_KEY` to a stable Fernet key and configure a Google OAuth application with `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` and an exact `YOUTUBE_REDIRECT_URI`. Keep the key private and backed up; losing it makes existing accounts unrecoverable. The callback host and browser host must match for the anonymous session cookie. Account disconnection erases stored credentials locally; revoke access in Google separately if desired.

For the one-click flow, connect one YouTube channel and generate Shorts from a source whose rights declaration you have made. The results page has **Schedule Shorts on YouTube**. One click queues Short 1 immediately, Short 2 for five hours later, and Short 3 for ten hours later. YouTube receives future videos as private uploads with native `publishAt` times. A second click reuses the same queue records; already uploaded Shorts are not uploaded again. The queue records `source_declaration` as the approval basis. This records the user's declaration, not an independent rights or music check. The older manual draft and review workflow remains available through the API.

Optional Groq integration uses `GROQ_API_KEY` from the ignored local `.env`. It transcribes each selected clip with Whisper segment timestamps for better burned-in captions and generates grounded titles, descriptions, and three to five relevant hashtags. Local captions and existing metadata remain the fallback if Groq is unavailable or rate limited. Existing Shorts get Groq metadata when the one-click scheduling action is used. Groq account limits and pricing can change; check your account before enabling it. After changing `.env`, restart the Celery worker and Django server.

### Connect a YouTube channel locally

1. In [Google Cloud](https://console.cloud.google.com/), create/select a project and enable **YouTube Data API v3**. In Google Auth Platform, configure Branding and an **External** Audience for a personal Google account. While the app is in Testing, add your Google account as a test user. In Data Access add `youtube.upload` and `youtube.readonly`.
2. In Google Auth Platform → Clients, create an OAuth client of type **Web application**. Add this exact **Authorized redirect URI**: `http://localhost:8000/api/v1/social/youtube/callback`. Use `localhost` for the frontend too (`http://localhost:5173` or `http://localhost:5174`). If you use `127.0.0.1` in the browser instead, use it consistently in the redirect URI and `.env`.
3. Generate a Fernet key with `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. In the local `.env`, set `SOCIAL_ENCRYPTION_KEY` to that value, set `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` from the new client, and set `YOUTUBE_REDIRECT_URI` to the exact registered URI. Set `SOCIAL_FRONTEND_URL` to the frontend address you use. Do not commit `.env` or change the Fernet key after connecting.
4. Restart Django and the Celery worker after editing `.env`; both load it automatically. Keep Redis running. In the studio open **Schedule → Connect YouTube**, sign in with the added test user, choose the channel and approve the two requested scopes. The connected channel then appears under Connected accounts.

Google's [Testing audience](https://support.google.com/cloud/answer/15549945) grants expire after seven days, so a test connection may require reconnection. Production OAuth verification is separate from local testing. The [YouTube Data API revision history](https://developers.google.com/youtube/data/revision_history) states that, since 2 November 2026, uploads from unverified API projects are no longer automatically forced private; YouTube account settings and other policy checks can still affect the result.

The older manual draft workflow still requires source-permission and music-rights checks before approval. YouTube uploads use the [official resumable API](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert); scheduled publication sends a future `status.publishAt` with `privacyStatus=private` as required by [YouTube's resource documentation](https://developers.google.com/youtube/data/reference/rest/v3/videos). Immediate uploads request public privacy; if the API keeps a video private, the queue marks it `uploaded_private` rather than claiming publication. A 5-minute Celery Beat status poll can confirm when the native schedule becomes public; run `celery -A backend beat` as an additional always-on service. OAuth verification and upload quota may still constrain actual publishing. The OAuth flow requests `youtube.upload` and `youtube.readonly` for channel identification and status checks. Live OAuth and uploads were **not** tested in this change. Review a test upload in a controlled channel before relying on this feature.

Instagram connection and Reel publishing are deliberately disabled. Meta's [official API documentation](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api) requires an eligible professional account and a publicly reachable video URL for the media container; this loopback deployment has neither. No Reel or connected account is simulated. Local scheduling also cannot run while the computer or worker is off. A zero-subscription workflow can still incur electricity, storage, network, quota and, if explicitly enabled, inference costs.

## API

Interactive OpenAPI: `http://localhost:8000/api/docs`.

1. `GET /api/v1/session` creates the private browser session and returns `csrf_token`.
2. Send cookies and `X-CSRFToken` for mutating requests.
3. `POST /api/v1/jobs` JSON example:

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID_11",
  "rights_declaration": true,
  "permission_reference": "Owned original recording",
  "options": {
    "caption_style": "minimal",
    "require_captions": true,
    "target_duration": 30,
    "resolution": "720",
    "layout": "preserve",
    "mode": "source_highlight"
  }
}
```

Use a real 11-character source ID. `POST /api/v1/uploads` accepts multipart `file`, `rights_declaration`, `permission_reference` and JSON-string `options`. Uploaded media is probed in the worker; extension alone is never trusted.

`GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/results`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry`, `POST /jobs/{id}/regenerate`, `POST /jobs/{id}/schedule-batch`, `GET /jobs/{id}/download` (all beneath `/api/v1`). Poll status every two seconds. `GET /jobs/{id}/analysis` exposes the saved candidate scores and sampled signals; routine polling omits those larger debug arrays. Progress is stage-based, with no invented ETA; cancellation takes effect at the next boundary, not during an active FFmpeg command. Polling is used instead of SSE.

`GET /capabilities` reports available provider modes. `GET /social/accounts`, `/social/posts`, `/social/preferences`, `/social/suggestions`, and YouTube OAuth connect/callback expose the queue. `POST /social/posts` saves a draft; `PUT /social/posts/{id}` edits it; `POST /social/posts/{id}/approve` uploads after review. `GET /shorts/{id}`, `/metadata`, `/video`, `/thumbnail`, `/download`, `/captions?format=srt|ass`; `POST /shorts/{id}/metadata` requests new grounded metadata. The transcript editor can re-render an individual Short while source footage is retained; full-clip regeneration remains a job-level action. `GET /health` reports process/tool/configuration availability, not worker readiness.

`POST /api/generate-short` is retained as a deprecated alias for the new rights-aware asynchronous schema. The old blocking `{url}` contract is intentionally retired because it lacks the required rights declaration.

## Verification

```sh
DJANGO_DEBUG=true .venv/bin/python backend/manage.py test api
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

Backend integration creates a synthetic 62-second test pattern, encodes three distinct 720×1280 MP4s, decodes/probes each and checks cleanup/idempotency. A separate integration test burns actual animated ASS captions; it explicitly skips if libass is absent. No copyrighted YouTube footage is required.

With API, Redis and Celery running, exercise the entire asynchronous HTTP path:

```sh
.venv/bin/python scripts/smoke.py
```

This creates an explicitly titled `SYNTHETIC-pipeline-test.mp4` history job, verifies immediate acceptance, follows actual worker progress, downloads three real MP4s and validates the batch ZIP. It does not present generated test patterns as actual user footage.

## Retention and deployment

```sh
.venv/bin/python backend/manage.py cleanup_studio
# Docker:
docker compose exec api python manage.py cleanup_studio
```

Schedule cleanup hourly. Source retention defaults to one day; terminal output retention to seven days. Cleanup marks abandoned jobs older than the job limit plus two minutes failed, prunes source directories and deletes expired terminal jobs/assets. Intermediate audio and partial renders are deleted in `finally`; source footage remains only until retention. Restarting or crashing a worker can leave a job in progress until this maintenance command runs. Losing browser session cookies loses access to its anonymous-session history.

For a reproducible local container stack:

```sh
cp .env.example .env
# Set a unique secret; local HTTP requires DJANGO_DEBUG=true for non-secure local cookies.
docker compose up --build
```

Open `http://localhost:8080`. Redis isn't exposed; API isn't exposed directly; media is accessible only through ownership-checked API endpoints. Worker concurrency is one and its container is limited to two CPUs/2 GB RAM. The non-root API and worker share a private named volume, and a separate migration service runs first. Docker configuration is supplied but was **not run here** because the Docker CLI is unavailable.

For an Internet deployment, put this studio behind authenticated HTTPS access, set `DJANGO_DEBUG=false`, unique secret, exact hosts/origins and secure proxy settings. The included stack binds to loopback deliberately; anonymous browser sessions are not a full multi-user account system. Add durable account authentication and distributed admission/rate limits before public multi-tenant access. Use a restricted worker/egress policy, disk limits at the volume level, backups, monitored Redis and error monitoring. Do not expose Redis, `/data`, Django admin or media storage publicly. Review nginx upload/time limits to match backend limits.

Local runtime state lives in `backend/.local/studio.sqlite3`, outside the tracked original empty `db.sqlite3`.

Django's database uses `DATABASE_URL` via `dj-database-url`; PostgreSQL requires installing/pinning `psycopg[binary]` and supplying a production database URL. Compose intentionally uses SQLite for a single-worker initial deployment. An object-storage adapter, signed URLs, centralized daily cost accounting and production error-monitoring integration are not included. Dependency locks are committed for Python/npm; image tags are supplied rather than immutable production digests.

## Scope still open

This is a working source-highlight release, not the complete aspirational product in `description.md`. Licensed TTS, user-recorded narration, Instagram publishing, remote vision inference, subject tracking/smoothed crop paths, automatic word alignment, automatic removal of silence within a selected clip, contextual graphic overlays, scene-aware multi-shot reconstruction, per-Short re-encoding, object storage and verified legal/monetization determinations remain unimplemented. Interface/provider protocols reserve the relevant boundaries; unsupported modes are rejected rather than simulated.
