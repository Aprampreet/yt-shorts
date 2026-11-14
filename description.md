# YouTube Shorts Generator

## 1. Project overview

YouTube Shorts Generator is a two-part web application:

- A React/Vite frontend where a user pastes a YouTube URL.
- A Django backend exposing a Django Ninja API that downloads the source video, selects a high-motion section, creates a short MP4 clip, and returns a URL that the frontend can play and download.

The current implementation is a synchronous, local-development prototype. The backend performs downloading, frame analysis, and video encoding during the same HTTP request. It does not currently use a database model, background worker, authentication system, persistent job record, or machine-learning model.

The interface calls the result an “AI-generated short”, but the implemented selection algorithm is a computer-vision heuristic based on frame differences. No AI or deep-learning inference is currently present.

## 2. Current capabilities

### Implemented

1. Accept a URL in the React form.
2. Send the URL to `POST /api/generate-short`.
3. Download one video with `yt-dlp`.
4. Prefer a downloaded format with a maximum height of 720 pixels.
5. Analyze the downloaded file with OpenCV.
6. Estimate a high-motion starting point.
7. Extract approximately a 30-second segment with MoviePy.
8. Encode the segment as H.264 MP4.
9. Serve the generated file through Django’s development media route.
10. Display the result in an HTML video player.
11. Download the result from the browser.

### Not implemented

- User accounts or authorization.
- URL ownership verification.
- Job queues or asynchronous processing.
- Progress updates from the backend.
- Captions, subtitles, vertical reframing, face tracking, or audio enhancement.
- Actual AI/deep-learning ranking or scene understanding.
- Database records for source videos or generated shorts.
- Automatic cleanup or retention policies for media files.
- Production-grade error types and error status codes.
- Automated backend tests.
- A backend dependency lock file or requirements file.

## 3. Repository structure

```text
yt-shorts/
├── description.md
├── backend/
│   ├── manage.py                  Django command-line entry point
│   ├── db.sqlite3                 Default SQLite database file
│   ├── api/
│   │   ├── views.py               API schema, download, analysis, and generation logic
│   │   ├── urls.py                Mounts the Ninja API
│   │   ├── models.py              Currently empty; no application models
│   │   ├── tests.py               Placeholder test module
│   │   ├── admin.py               Placeholder admin module
│   │   ├── apps.py                Django app configuration
│   │   └── migrations/
│   └── backend/
│       ├── settings.py            Django configuration and media settings
│       ├── urls.py                Admin, API, and media routing
│       ├── asgi.py                ASGI entry point
│       └── wsgi.py                WSGI entry point
└── frontend/
    ├── package.json               JavaScript dependencies and scripts
    ├── vite.config.js             Vite, React, and Tailwind configuration
    ├── index.html                 Browser entry document
    └── src/
        ├── main.jsx               React bootstrap
        ├── App.jsx                Main page layout
        ├── index.css              Tailwind import
        ├── App.css                Legacy/template styles
        └── components/
            ├── api.jsx            Axios API client
            ├── input_card.jsx     URL input, request state, and result state
            ├── output_card.jsx    Video player and download button
            └── progressbar.jsx    Reusable progress UI, currently unused
```

Generated Python `__pycache__` files and the SQLite database are runtime artifacts, not application logic.

## 4. Architecture

```text
Browser
  │
  │ POST http://localhost:8000/api/generate-short
  │ JSON: { "url": "<YouTube URL>" }
  ▼
React input_card.jsx
  │
  ▼
frontend/src/components/api.jsx
  │ Axios
  ▼
Django URL router: /api/
  │
  ▼
Django Ninja endpoint: /generate-short
  │
  ├─ yt-dlp downloads source video into backend/media/
  ├─ OpenCV reads frames and calculates motion scores
  ├─ MoviePy opens the source and extracts a segment
  ├─ MoviePy/FFmpeg writes *_short.mp4 into backend/media/
  └─ Django builds an absolute /media/... URL
  │
  ▼
React output_card.jsx
  ├─ Plays the returned MP4
  └─ Fetches the MP4 as a Blob for download
```

The browser and backend are separate development servers. The frontend is normally served by Vite on port 5173 and the backend by Django on port 8000. The frontend has the backend URL hard-coded as `http://localhost:8000/api`.

## 5. Backend request workflow

The endpoint is defined in `backend/api/views.py` and is exposed by:

1. `backend/backend/urls.py` includes `api.urls` under `/api/`.
2. `backend/api/urls.py` mounts the Ninja API at the application root.
3. The Ninja API registers `POST /generate-short`.
4. The complete URL is therefore `POST /api/generate-short`.

### Request validation

The endpoint uses this Pydantic schema:

```json
{
  "url": "https://www.youtube.com/watch?v=example"
}
```

The schema requires a string field named `url`, but it does not currently validate that the value is a supported YouTube URL, that it is non-empty, or that it is safe to process.

### Download phase

The endpoint creates this `yt-dlp` output template:

```text
<MEDIA_ROOT>/%(title)s.%(ext)s
```

The configured options are:

- `format: best[height<=720]`: select the best available format whose height is no more than 720 pixels.
- `outtmpl`: save into Django’s media directory using the source title.
- `noplaylist: True`: process one video rather than a playlist.

`yt_dlp.YoutubeDL(...).extract_info(url, download=True)` both retrieves metadata and downloads the media.

### File lookup phase

The source title is sanitized by removing these characters:

```text
< > : " / \ | ? *
```

The backend then expects the source file to be:

```text
<MEDIA_ROOT>/<sanitized-title>.mp4
```

If that exact path does not exist, the endpoint returns an error object. This lookup assumes that `yt-dlp` produced an MP4 file and that its final filename matches the backend’s sanitization logic. That assumption can fail for formats, title normalization, duplicate names, or downloads that use a different extension.

### Motion analysis

`find_best_start(filepath)` opens the source file with OpenCV:

1. Read frames until the video ends.
2. Convert every frame to grayscale.
3. Apply a median blur.
4. Compute the absolute difference between the grayscale frame and its blurred version.
5. Sum the difference values to obtain a motion/detail score.
6. Store one score per frame.
7. Select the frame with the largest score.
8. Divide its frame index by `30` to estimate seconds.

The result is called `best_start`. The `30` divisor is an approximation and assumes a 30 FPS source. The actual FPS is not read from the video, so the estimated timestamp can be inaccurate for videos with another frame rate.

This algorithm detects local visual detail or high-frequency content, not semantic importance. It may select a flash, transition, noisy frame, text-heavy frame, camera shake, or other visually sharp moment rather than the most interesting story segment.

### Clip generation

The source is opened with `moviepy.VideoFileClip`. If the selected timestamp is within the final 30 seconds, it is capped so that the requested segment does not extend beyond the end:

```text
best_start = min(best_start, clip.duration - 30)
```

The code then requests:

```text
clip.subclipped(best_start - 2, best_start + 30)
```

This attempts to include two seconds before the detected moment and continue for thirty seconds after it. In practice, the produced clip can be close to 32 seconds, and a negative start time can occur when the selected point is near the beginning. The current implementation does not explicitly clamp the lower bound to zero or close the MoviePy clips after encoding.

The output filename is:

```text
<sanitized-title>_short.mp4
```

The output is encoded with the `libx264` codec. MoviePy delegates the actual media work to FFmpeg.

### Successful response

On success, the endpoint returns JSON similar to:

```json
{
  "short_url": "http://localhost:8000/media/Example%20Video_short.mp4",
  "title": "Example Video",
  "duration": 32
}
```

- `short_url`: absolute URL generated with Django’s `request.build_absolute_uri`.
- `title`: original title from `yt-dlp`.
- `duration`: integer duration reported by MoviePy.

### Error response

The entire processing body is currently wrapped in a broad exception handler. Errors are returned as a successful-looking JSON response with an `error` field:

```json
{
  "error": "description of the exception"
}
```

The endpoint does not currently return appropriate HTTP status codes such as `400`, `404`, `422`, or `500`. Error messages may expose internal paths or dependency details and should not be returned directly in a production deployment.

## 6. Frontend workflow

### Application bootstrap

`frontend/src/main.jsx`:

1. Imports Tailwind CSS through `index.css`.
2. Creates a React root from the `#root` element in `index.html`.
3. Renders `App` inside React `StrictMode`.

`App.jsx` renders the dark gradient page and the `AIShortGeneratorCard`. `OutputCard` and `ProgressBar` are imported by `App`, but only `AIShortGeneratorCard` is rendered there. The result card is rendered by `input_card.jsx` after a successful request.

### Input component state

`AIShortGeneratorCard` maintains:

- `videoUrl`: value entered by the user.
- `shortUrl`: returned media URL.
- `title`: returned source title.
- `duration`: returned result duration.
- `error`: displayed error text.

When the button is clicked:

1. Clear the previous error.
2. Clear the previous result URL.
3. Call `generateShort(videoUrl)`.
4. If `short_url` exists, store the response fields and render `OutputCard`.
5. Otherwise display `response.error`, or a generic failure message.

There is no client-side URL validation, disabled/loading state, request cancellation, retry control, or `try/catch` around the Axios call. A network rejection can therefore become an unhandled promise rejection rather than a visible application error.

### API client

`frontend/src/components/api.jsx` defines:

```text
API_URL = http://localhost:8000/api
```

`generateShort(video_url)` sends:

```http
POST /api/generate-short
Content-Type: application/json
```

with:

```json
{ "url": "<video_url>" }
```

It returns `response.data` without transforming or validating the response shape.

### Output component

`OutputCard`:

- Displays the source title.
- Displays the returned duration when present.
- Plays the generated URL in a muted, looping HTML `<video>` element.
- Creates an anchor element when the user selects Download.
- Fetches the media URL as a Blob.
- Creates a temporary object URL.
- Starts a download named `AI_Short.mp4`.
- Revokes the temporary object URL after starting the download.

The download fetch has no error handler. Cross-origin behavior must also remain compatible with the backend’s CORS and media response configuration.

### Progress component

`ProgressBar` is a reusable visual component that accepts a `progress` prop, animates a local copy of that value, and renders a percentage. It is not connected to the generation request and is not currently rendered in the active page. Processing is synchronous from the browser’s perspective, so the UI cannot report actual backend progress.

## 7. Configuration

### Django settings

The backend uses:

- Django 5.1-generated settings structure.
- SQLite at `backend/db.sqlite3`.
- `DEBUG = True`.
- Empty `ALLOWED_HOSTS`.
- `CORS_ALLOW_ALL_ORIGINS = True`.
- `MEDIA_URL = '/media/'`.
- `MEDIA_ROOT = <backend directory>/media`.
- UTC timezone.
- Django admin, auth, sessions, static files, `corsheaders`, `ninja`, and `api` installed.

Development media serving is enabled in `backend/backend/urls.py` with Django’s `static()` helper. This is appropriate for local development only and should be replaced by a proper media server or object storage in production.

### Frontend settings

The Vite configuration enables:

- React Fast Refresh through `@vitejs/plugin-react`.
- Tailwind CSS through `@tailwindcss/vite`.

The frontend package provides:

- `npm run dev`: start the Vite development server.
- `npm run build`: create a production build.
- `npm run lint`: run ESLint.
- `npm run preview`: serve the built frontend locally.

There is currently no environment-variable-based API URL. Deployment to another host requires changing the hard-coded URL or adding a Vite environment variable.

## 8. Local setup and execution

### Prerequisites

Install and make available:

- Python compatible with the installed Django project.
- Node.js and npm.
- FFmpeg available on `PATH`; MoviePy requires it for video encoding.
- Python packages imported by the backend:
  - Django
  - django-cors-headers
  - django-ninja
  - pydantic
  - yt-dlp
  - moviepy
  - opencv-python
  - numpy

The repository does not currently contain `requirements.txt`, `pyproject.toml`, `Pipfile`, or another backend dependency manifest. Create and maintain one before sharing or deploying the project.

### Start the backend

From the repository root:

```bash
cd backend
python -m pip install Django django-cors-headers django-ninja pydantic yt-dlp moviepy opencv-python numpy
python manage.py migrate
python manage.py runserver 8000
```

The API should then be available at:

```text
http://localhost:8000/api/generate-short
```

The first request may create the `media/` directory only if it already exists through the surrounding environment; verify that the directory is writable before processing videos.

### Start the frontend

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal, normally `http://localhost:5173`.

### Direct API test

With the backend running, a request can be made with:

```bash
curl -X POST http://localhost:8000/api/generate-short \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=VIDEO_ID"}'
```

This starts a real download and video encode. It may take substantially longer than a normal API request and requires network access, a valid source URL, disk space, and FFmpeg.

## 9. End-to-end data flow

1. The user enters a source URL in the browser.
2. React stores the value in component state.
3. The submit handler calls Axios.
4. Django receives JSON and Django Ninja parses the `url` field.
5. `yt-dlp` resolves the source metadata and downloads the selected media.
6. The backend derives a sanitized source filename.
7. OpenCV scans every frame and keeps motion/detail scores in memory.
8. The highest-scoring frame becomes the approximate segment anchor.
9. MoviePy opens the source and creates a subclip.
10. FFmpeg encodes the result as H.264 MP4 in `MEDIA_ROOT`.
11. Django returns an absolute media URL.
12. React stores the response and renders the output card.
13. The browser streams the MP4 through the `<video>` element.
14. A separate browser fetch converts the file into a Blob for download.

## 10. Performance and resource behavior

The current implementation is resource-intensive:

- The HTTP request remains open for the entire download, frame scan, and encoding process.
- OpenCV reads every frame and retains one numeric score per frame.
- MoviePy and FFmpeg require CPU, memory, temporary files, and disk space.
- Multiple users can exhaust server workers, memory, CPU, bandwidth, or storage.
- The source and generated files remain on disk because no cleanup runs.
- Reusing the same title can overwrite or conflict with an existing file.

For production, processing should be moved to a background job system such as Celery/RQ with Redis or another queue. The API should return a job ID immediately, and the frontend should poll a job-status endpoint or receive server-sent/WebSocket events. Source and output files should use unique IDs rather than titles.

## 11. Security and reliability considerations

The current development configuration must not be deployed unchanged.

### High-priority changes

1. Move `SECRET_KEY` to an environment variable and rotate the exposed development key.
2. Set `DEBUG = False` in production.
3. Configure an explicit `ALLOWED_HOSTS` list.
4. Replace `CORS_ALLOW_ALL_ORIGINS = True` with the real frontend origin.
5. Validate and restrict accepted source URLs.
6. Add request size, duration, resolution, timeout, and rate limits.
7. Avoid returning raw exception text, internal file paths, or command details.
8. Use structured error responses with correct HTTP status codes.
9. Store generated files under unique, controlled paths.
10. Add cleanup and retention policies for downloaded and generated media.
11. Run processing with a restricted service account and limited filesystem permissions.
12. Use a production media server or object storage instead of Django development media serving.
13. Check the licensing and terms of service for every source and generated use case.

### Correctness issues to address

- Use the actual video FPS instead of dividing frame indexes by a fixed `30`.
- Handle videos with zero readable frames before calling `max()` or `index()`.
- Clamp the clip start to `0`.
- Clamp the clip end to the actual duration.
- Handle source videos shorter than the requested clip length.
- Close `VideoFileClip` and output resources reliably with context-management or `finally`.
- Resolve the actual downloaded filename from `yt-dlp` metadata instead of reconstructing it.
- Avoid title collisions and unsafe/unportable filenames.
- Catch Axios/network failures in the frontend.
- Add a visible loading state and prevent duplicate submissions.
- Make the API base URL configurable.
- Ensure output media is accessible under the deployed frontend origin.

## 12. Testing strategy

The repository currently has only placeholder Django tests and no frontend test suite. A practical test plan is:

### Backend unit tests

- `sanitize_filename` removes forbidden characters.
- URL validation accepts supported URLs and rejects malformed or unsupported values.
- Motion analysis handles an empty or unreadable video safely.
- Start and end timestamps are clamped correctly.
- Short source videos do not produce invalid subclip ranges.
- Generated filenames are unique and remain inside `MEDIA_ROOT`.

### Backend integration tests

- `POST /api/generate-short` rejects invalid JSON.
- A mocked `yt-dlp` download produces the expected response shape.
- A mocked video-processing pipeline returns a media URL and duration.
- Processing failures return stable error status codes.
- Media URLs are served only according to the intended access policy.

### Frontend tests

- The input value is captured correctly.
- Successful API responses render the title, duration, and video.
- API error objects render a useful message.
- Network failures render a useful message.
- Submit is disabled while processing.
- Download failures are visible to the user.

### Manual smoke test

1. Start Django and Vite.
2. Paste a valid public video URL.
3. Confirm one request reaches `/api/generate-short`.
4. Confirm a generated MP4 appears in `backend/media/`.
5. Confirm the response contains `short_url`, `title`, and `duration`.
6. Confirm the browser can play the returned video.
7. Confirm Download saves an MP4 file.
8. Inspect server logs for warnings and unhandled exceptions.

## 13. Recommended production workflow

```text
1. Validate URL and create a job record
2. Return job_id with HTTP 202
3. Queue download and processing task
4. Download into a unique temporary directory
5. Validate file type, size, duration, and resolution
6. Analyze frames using bounded sampling
7. Generate and validate the final clip
8. Store output in object storage or controlled media storage
9. Update job status and output metadata
10. Delete temporary source files
11. Expire old outputs according to retention policy
12. Let the frontend poll status and render progress/errors
```

Suggested job states are:

```text
queued -> downloading -> analyzing -> encoding -> completed
                                      └───────> failed
```

Suggested API surface:

```text
POST /api/jobs
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/result
DELETE /api/jobs/{job_id}
```

## 14. Suggested implementation roadmap

### Phase 1: Stabilize the prototype

- Add a backend dependency manifest.
- Add environment-based settings.
- Create the media directory explicitly during setup.
- Add input validation and correct HTTP error responses.
- Fix timestamp bounds, FPS handling, resource closing, and filename resolution.
- Add frontend loading and network-error handling.
- Remove unused imports and connect or remove the unused progress component.

### Phase 2: Make processing reliable

- Add automated backend and frontend tests.
- Add structured logging.
- Add unique job and file identifiers.
- Add storage cleanup.
- Add bounded processing limits.
- Add a queue worker for long-running video work.

### Phase 3: Improve product behavior

- Add real progress reporting.
- Add configurable clip duration and aspect ratio.
- Add vertical 9:16 composition for Shorts.
- Add captions and safe-area handling.
- Add scene and speech analysis if an actual AI model is required.
- Add authentication, user history, and quota controls.

### Phase 4: Deploy safely

- Build the frontend with a production API URL.
- Run Django with a production WSGI/ASGI server.
- Put a reverse proxy in front of the application.
- Serve media from object storage or a dedicated media service.
- Configure HTTPS, CORS, host validation, secrets, monitoring, backups, and alerts.

## 15. Important distinction: current behavior versus product claims

The UI copy currently says “AI-powered editing”, “AI Cinematic Shorts”, and “deep-learning optimization”. The source code currently implements:

- URL submission.
- `yt-dlp` downloading.
- OpenCV frame-level visual-difference scoring.
- MoviePy/FFmpeg clipping and encoding.

It does not implement deep learning, language understanding, speech analysis, caption generation, semantic scene ranking, or automatic vertical composition. Documentation, UI copy, and product expectations should be aligned with the actual implementation or the missing AI pipeline should be added deliberately.

