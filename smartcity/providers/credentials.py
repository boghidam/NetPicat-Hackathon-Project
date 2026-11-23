"""Local credential storage for Tranzy / partner API keys (stored in git-ignored JSON or env vars)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..slots import atomic_write_json

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "local_credentials.json"
DEFAULT_TRANZY_AGENCY_ID = "1"  # CTP Iași agency ID on Tranzy Open Data


def load_credentials(path: Path = CREDENTIALS_PATH) -> dict[str, str]:
    """Load Tranzy API credentials from local file, falling back to environment variables."""
    api_key = os.environ.get("TRANZY_API_KEY", "").strip()
    agency_id = os.environ.get("TRANZY_AGENCY_ID", DEFAULT_TRANZY_AGENCY_ID).strip() or DEFAULT_TRANZY_AGENCY_ID
    cors_proxy = os.environ.get("TRANZY_CORS_PROXY", "").strip()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                api_key = str(raw.get("tranzy_api_key", api_key) or "").strip()
                agency_id = str(raw.get("tranzy_agency_id", agency_id) or DEFAULT_TRANZY_AGENCY_ID).strip()
                cors_proxy = str(raw.get("cors_proxy_url", cors_proxy) or "").strip()
        except (OSError, ValueError):
            pass
    return {
        "tranzy_api_key": api_key,
        "tranzy_agency_id": agency_id,
        "cors_proxy_url": cors_proxy,
    }


def save_credentials(
    api_key: str,
    agency_id: str = DEFAULT_TRANZY_AGENCY_ID,
    cors_proxy_url: str = "",
    path: Path = CREDENTIALS_PATH,
) -> dict[str, str]:
    """Atomically store local API credentials."""
    payload = {
        "tranzy_api_key": api_key.strip(),
        "tranzy_agency_id": agency_id.strip() or DEFAULT_TRANZY_AGENCY_ID,
        "cors_proxy_url": cors_proxy_url.strip(),
    }
    atomic_write_json(path, payload)
    return payload
