import json

from fastapi import APIRouter, Header, HTTPException, Request

from app.settings import get_settings
from app.webhooks.hubspot import normalize_events, process_hubspot_identity_events, verify_request_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
_SEEN_MESSAGE_IDS: set[str] = set()


@router.post("/hubspot")
async def hubspot_webhook(
    request: Request,
    x_hubspot_signature_v3: str | None = Header(default=None, alias="X-HubSpot-Signature-v3"),
    x_hubspot_request_timestamp: str | None = Header(default=None, alias="X-HubSpot-Request-Timestamp"),
) -> dict[str, object]:
    payload = await request.body()
    if not x_hubspot_signature_v3 or not x_hubspot_request_timestamp:
        raise HTTPException(status_code=401, detail="missing signature")

    settings = get_settings()
    secret = settings.hubspot_client_secret
    if not secret:
        raise HTTPException(status_code=401, detail="webhook secret not configured")
    if not verify_request_signature(request, payload, x_hubspot_request_timestamp, x_hubspot_signature_v3, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        body_json = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body_json = None
    message_id, events = normalize_events(body_json)

    if message_id and message_id in _SEEN_MESSAGE_IDS:
        return {"status": "duplicate", "messageId": message_id}
    try:
        from app.db.repo import Repo

        repo = Repo()
        if message_id and repo.seen_webhook(message_id):
            _SEEN_MESSAGE_IDS.add(message_id)
            return {"status": "duplicate", "messageId": message_id}
        if message_id:
            repo.mark_webhook(message_id)
            _SEEN_MESSAGE_IDS.add(message_id)
        repo.add_event(
            "webhook",
            "INFO",
            "HubSpot webhook accepted",
            {"messageId": message_id, "events": len(events)},
        )
    except Exception:
        if message_id:
            _SEEN_MESSAGE_IDS.add(message_id)

    processed = process_hubspot_identity_events(events)
    return {
        "status": "accepted",
        "payload_bytes": str(len(payload)),
        "processed": processed.get("processed", 0),
        "messageId": message_id,
    }
