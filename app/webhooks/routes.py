import json

from fastapi import APIRouter, Header, HTTPException, Request

from app.settings import get_settings
from app.webhooks.hubspot import verify_hubspot_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
_SEEN_MESSAGE_IDS: set[str] = set()


@router.post("/hubspot")
async def hubspot_webhook(
    request: Request,
    x_hubspot_signature_v3: str | None = Header(default=None, alias="X-HubSpot-Signature-v3"),
    x_hubspot_request_timestamp: str | None = Header(default=None, alias="X-HubSpot-Request-Timestamp"),
) -> dict[str, str]:
    payload = await request.body()
    if not x_hubspot_signature_v3 or not x_hubspot_request_timestamp:
        raise HTTPException(status_code=401, detail="missing signature")

    try:
        body_json = json.loads(payload.decode("utf-8"))
        message_id = body_json.get("messageId")
        events = body_json if isinstance(body_json, list) else body_json.get("events") or [body_json]
    except (ValueError, UnicodeDecodeError):
        message_id = None
        events = []

    method = request.method.encode("utf-8")
    uri = str(request.url.path).encode("utf-8")
    settings = get_settings()
    secret = settings.hubspot_client_secret or "dev-secret"
    if not verify_hubspot_signature(method, uri, payload, x_hubspot_request_timestamp, x_hubspot_signature_v3, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    if message_id and message_id in _SEEN_MESSAGE_IDS:
        return {"status": "duplicate", "messageId": message_id}
    if message_id:
        _SEEN_MESSAGE_IDS.add(message_id)

    try:
        from app.db.repo import Repo

        Repo().add_event("webhook", "INFO", "HubSpot webhook accepted", {"messageId": message_id, "events": len(events) if isinstance(events, list) else 1})
    except Exception:
        pass
    return {"status": "accepted", "payload_bytes": str(len(payload))}
