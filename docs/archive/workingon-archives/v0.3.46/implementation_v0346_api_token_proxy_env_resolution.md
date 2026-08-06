# v0.3.46 implementation archive

## Scope

Fixed the local-development proxy token mismatch that caused the frontend to ask for an API token in the middle of user flows.

## Product Change

- The server-side Next.js proxy now searches upward from `process.cwd()` for a local `.env`.
- The proxy reads `API_TOKEN` from local `.env` when the frontend process does not already have `process.env.API_TOKEN`.
- The proxy reads `AGENT_PLATFORM_URL` from local `.env` when the frontend process does not already have `process.env.AGENT_PLATFORM_URL`.
- Browser-provided token remains highest priority for explicit manual overrides.
- `frontend_token` continues to be stripped from backend query strings before forwarding.

## Files

- `platform/frontend/app/api/platform/[...path]/route.ts`
- `scripts/v03_46_api_token_proxy_env_resolution.py`
- `tests/test_v03_46_api_token_proxy_env_resolution.py`
- `docs/testing/regression_lanes.json`

## Verification

- `tests/test_v03_46_api_token_proxy_env_resolution.py`: `6 passed`
- Current v0.3.x release gate: `256 passed, 1 warning`
- Evidence: `docs/workingon-archives/v0.3.46/api_token_proxy_env_resolution_v0.3.46.json`

