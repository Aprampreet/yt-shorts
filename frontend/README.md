# Framecut Studio frontend

React 19 + Vite + Tailwind, using the existing application structure. See the [root README](../README.md) for backend, worker, inference, rights review and deployment setup.

```sh
npm ci
npm run dev
npm test
npm run lint
npm run build
```

The development proxy sends `/api` to `127.0.0.1:8000`. The UI uses real API state and session-owned assets. No preview videos, generation progress or AI results are fabricated. With no backend, the UI shows a connection error and disables submission.
