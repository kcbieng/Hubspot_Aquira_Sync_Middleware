import json
import logging

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.settings import get_settings
from app.webhooks.aquira import extract_notification, message_id_for, parse_body
from app.sync.worker import enqueue_aquira_notification, enqueue_hubspot_identity
from app.webhooks.hubspot import normalize_events, verify_request_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])
_SEEN_MESSAGE_IDS: set[str] = set()


def _authorized_aquira(request: Request, token: str | None, header_token: str | None, authorization: str | None) -> bool:
    secret = (get_settings().aquira_webhook_secret or "").strip()
    if not secret:
        return True
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    offered = (token or header_token or bearer or "").strip()
    return offered == secret


@router.api_route("/aquira", methods=["GET", "POST", "PUT"])
async def aquira_webhook(
    request: Request,
    token: str | None = Query(default=None),
    x_hubquira_token: str | None = Header(default=None, alias="X-HubQuira-Token"),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    if not _authorized_aquira(request, token, x_hubquira_token, authorization):
        raise HTTPException(status_code=401, detail="invalid aquira webhook token")
    payload = await request.body()
    grouped: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        grouped.setdefault(key, []).append(value)
    parsed = parse_body(payload, request.headers.get("content-type"), grouped)
    raw_text = payload.decode("utf-8", "replace") if payload else request.url.query
    extracted = extract_notification(parsed, raw_text)
    message_id = message_id_for(payload or raw_text.encode(), extracted)

    settings = get_settings()
    duplicate = False
    try:
        from app.db.repo import Repo

        repo = Repo()
        if repo.seen_webhook(message_id):
            duplicate = True
        else:
            repo.mark_webhook(message_id)
            repo.add_event(
                "webhook",
                "INFO",
                "Aquira notification accepted",
                {
                    "messageId": message_id,
                    "method": request.method,
                    "contentType": request.headers.get("content-type"),
                    "extracted": extracted,
                    "headers": {
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() not in {"authorization", "cookie"}
                    },
                    "body": parsed if not isinstance(parsed, str) or len(parsed) < 4000 else parsed[:4000],
                    "raw": raw_text[:4000],
                },
            )
    except Exception:
        logger.exception("Could not persist Aquira webhook receipt")
        if message_id in _SEEN_MESSAGE_IDS:
            duplicate = True
        else:
            _SEEN_MESSAGE_IDS.add(message_id)

    if request.method == "GET" and not payload:
        return {"status": "ok", "service": "HubQuira", "hint": "POST Aquira notifications to this URL"}

    if not duplicate and (extracted.get("ids") or extracted.get("contract_cds")):
        enqueue_aquira_notification(extracted, whatif=bool(settings.whatif))

    return {
        "status": "duplicate" if duplicate else "accepted",
        "messageId": message_id,
        "extracted": extracted,
        "bytes": len(payload),
    }


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

    processed = enqueue_hubspot_identity(events)
    return {
        "status": "accepted",
        "payload_bytes": str(len(payload)),
        "processed": processed.get("events", 0),
        "queued": True,
        "messageId": message_id,
    }
