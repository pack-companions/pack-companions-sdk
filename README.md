# pack-companions-sdk

Python SDK for the [Pack Companions Service](https://github.com/getpacked/pack-companions) — AI companions across the Pack ecosystem.

## What this is

A thin, async Python client that lets your FastAPI (or any Python) app talk to the Pack Companions service without dealing with HMAC signing, retries, or the WebSocket dance manually.

If you're consuming companions from a frontend, see [`@getpacked/companions`](https://github.com/getpacked/pack-companions-js) instead.

## Install

```bash
# From PyPI (once published)
pip install pack-companions-sdk

# Local dev (editable install — changes show up live)
pip install -e ~/projects/pack-companions-sdk/
```

## Quickstart

```python
from pack_companions import CompanionsClient

client = CompanionsClient(
    api_key="your_app_api_key",
    secret="your_app_hmac_secret",
    service_url="http://localhost:9200",  # or production URL
)

# Real methods land in Phase 1 (post-Phase-0b auth)
```

## Versioning

This SDK follows independent semver from the service.

| SDK version | Minimum service version |
|---|---|
| 0.0.x | 0.0.x (Phase 0a/0b — pre-release) |

A compatibility table will be maintained here as the service stabilizes.

## Status

🟢 **Phase 0a — Shell** (current — no methods implemented yet)
⚪ Phase 0b — Auth + HMAC signing
⚪ Phase 1 — Comment + opportunity methods
⚪ Phase 4 — Conversation methods

## License

UNLICENSED — internal GetPacked / Pack ecosystem SDK.
