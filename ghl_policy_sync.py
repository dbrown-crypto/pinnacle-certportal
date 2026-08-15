"""Authenticated, idempotent GoHighLevel -> portal policy-line sync.

GHL stores Auto, Cargo and GL as separate Policy custom-object records. The
portal stores one combined snapshot per customer. This module merges each GHL
line into that snapshot; it never issues a certificate or creates a customer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import hmac
import json
import logging
import os
import re
import uuid
from typing import Optional

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from acord25_2016_overlay import CarrierIdentityError, _naic_for


GHL_POLICY_NAMESPACE = uuid.UUID("91ae37d2-1ca5-4a58-bd2f-a213f65e9643")
GHL_API_BASE = "https://services.leadconnectorhq.com"
ALLOWED_AUTO_SYMBOLS = {"ANY", "OWNED", "SCHEDULED", "HIRED", "NON-OWNED"}
logger = logging.getLogger(__name__)


def _normalize_ghl_bool(value):
    """Normalize GHL checkbox serializations before Pydantic validates them."""
    if isinstance(value, list):
        if not value:
            return False
        value = value[0] if len(value) == 1 else value
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return False
        if normalized in {"false", "0", "no", "off", "disabled", "unchecked"}:
            return False
        # GHL may send the selected checkbox label rather than a boolean.
        return True
    return value


def _normalize_ghl_money(value):
    """Strip GHL currency formatting while leaving validity to the schema."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return value


def _normalize_ghl_multi(value) -> list[str]:
    """Accept GHL arrays, JSON arrays, or comma-delimited checkbox text."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            value = decoded if isinstance(decoded, list) else value
        except json.JSONDecodeError:
            pass
        if isinstance(value, str):
            value = value.split(",")
    if not isinstance(value, (list, tuple, set)):
        return value
    normalized: list[str] = []
    for item in value:
        symbol = str(item).strip().upper()
        if symbol in ALLOWED_AUTO_SYMBOLS and symbol not in normalized:
            normalized.append(symbol)
    return normalized


class PolicyLineSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ghl_policy_id: str = Field(min_length=1, max_length=200)
    portal_policy_id: Optional[uuid.UUID] = None
    named_insured: str = Field(min_length=1, max_length=300)
    insured_address: str = Field(min_length=1, max_length=1000)
    usdot: Optional[str] = Field(default=None, max_length=30)
    status: str
    self_serve_enabled: Optional[bool] = None
    effective_date: dt.date
    expiration_date: dt.date
    data_current_as_of: dt.date
    producer_block: Optional[str] = Field(default=None, max_length=1000)

    line_of_business: str = Field(min_length=1, max_length=100)
    carrier: str = Field(min_length=1, max_length=300)
    carrier_naic: Optional[str] = Field(default=None, max_length=10)
    policy_number: str = Field(min_length=1, max_length=100)
    coverage_limit: Optional[float] = Field(default=None, gt=0)
    aggregate_limit: Optional[float] = Field(default=None, gt=0)
    deductible: Optional[float] = Field(default=None, ge=0)
    auto_symbols: list[str] = Field(default_factory=list)
    gl_coverage_form: Optional[str] = Field(default=None, max_length=30)
    gl_aggregate_basis: Optional[str] = Field(default=None, max_length=30)

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z]", "", value.lower())
        aliases = {
            "active": "active", "inforce": "active", "inforced": "active",
            "cancelled": "cancelled", "canceled": "cancelled",
            "pending": "pending",
            "expired": "expired", "lapsed": "expired", "nonrenewed": "expired",
        }
        if normalized not in aliases:
            raise ValueError(
                "status must be Active, In Force, Pending, Cancelled, "
                "Expired, Lapsed, or Non-Renewed"
            )
        return aliases[normalized]

    @field_validator(
        "carrier_naic", "usdot", "producer_block",
        "gl_coverage_form", "gl_aggregate_basis", mode="before",
    )
    @classmethod
    def blank_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("coverage_limit", "aggregate_limit", "deductible", mode="before")
    @classmethod
    def blank_number_to_none(cls, value, info: ValidationInfo):
        normalized = _normalize_ghl_money(value)
        # GHL serializes an untouched optional Monetary field as "$0.00".
        # Aggregate is required only for active GL; for other lines, zero means
        # the field was not supplied rather than a real coverage value.
        if info.field_name == "aggregate_limit" and normalized == 0:
            return None
        return normalized

    @field_validator("self_serve_enabled", mode="before")
    @classmethod
    def normalize_checkbox(cls, value):
        return _normalize_ghl_bool(value)

    @field_validator("auto_symbols", mode="before")
    @classmethod
    def normalize_auto_symbols(cls, value):
        return _normalize_ghl_multi(value)

    @model_validator(mode="after")
    def policy_line_is_complete(self):
        if self.expiration_date < self.effective_date:
            raise ValueError("expiration_date cannot be before effective_date")
        line = _line_key(self.line_of_business)
        if self.status == "active" and line in {"auto", "cargo", "gl"}:
            if self.coverage_limit is None:
                raise ValueError("coverage_limit is required for an active Auto, Cargo, or GL line")
            if line == "gl" and self.aggregate_limit is None:
                raise ValueError("aggregate_limit is required for an active GL line")
        if self.status == "active" and line == "auto" and not self.auto_symbols:
            raise ValueError("auto_symbols is required for an active Auto line")
        if self.status == "active" and line == "gl":
            if not self.gl_coverage_form or not self.gl_aggregate_basis:
                raise ValueError("gl_coverage_form and gl_aggregate_basis are required for an active GL line")
        return self


def _verify_secret(authorization: Optional[str]) -> None:
    expected = os.environ.get("GHL_POLICY_SYNC_SECRET", "")
    if len(expected) < 32:
        raise HTTPException(503, "Policy sync is not configured.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing policy sync bearer token.")
    if not hmac.compare_digest(authorization.split(" ", 1)[1], expected):
        raise HTTPException(401, "Invalid policy sync bearer token.")


def _ghl_headers() -> dict[str, str]:
    token = os.environ.get("GHL_PRIVATE_INTEGRATION_TOKEN", "").strip()
    if len(token) < 20:
        raise HTTPException(503, "GoHighLevel association lookup is not configured.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Version": "2021-07-28",
    }


def _ghl_location_id() -> str:
    location_id = os.environ.get("GHL_LOCATION_ID", "").strip()
    if not location_id:
        raise HTTPException(503, "GoHighLevel association lookup is not configured.")
    return location_id


def _ghl_params() -> dict[str, object]:
    return {"locationId": _ghl_location_id(), "limit": 100, "skip": 0}


def _contact_ids_from_relations(payload: object, policy_id: str) -> set[str]:
    if isinstance(payload, list):
        relations = payload
    elif isinstance(payload, dict):
        relations = payload.get("relations") or payload.get("data") or []
    else:
        relations = []

    contact_ids: set[str] = set()
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        first_key = str(relation.get("firstObjectKey", "")).lower()
        second_key = str(relation.get("secondObjectKey", "")).lower()
        first_id = str(relation.get("firstRecordId", "")).strip()
        second_id = str(relation.get("secondRecordId", "")).strip()
        if first_id == policy_id and second_key in {"contact", "contacts"} and second_id:
            contact_ids.add(second_id)
        if second_id == policy_id and first_key in {"contact", "contacts"} and first_id:
            contact_ids.add(first_id)
    return contact_ids


async def _resolve_associated_contact_email(policy_id: str) -> str:
    headers = _ghl_headers()
    lookup_stage = "relations"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            relation_response = await client.get(
                f"{GHL_API_BASE}/associations/relations/{policy_id}",
                headers=headers,
                params=_ghl_params(),
            )
            relation_response.raise_for_status()
            contact_ids = _contact_ids_from_relations(relation_response.json(), policy_id)
            if len(contact_ids) != 1:
                raise HTTPException(
                    409,
                    "Exactly one Contact must be associated with this GoHighLevel Policy before syncing.",
                )
            lookup_stage = "contact"
            contact_response = await client.get(
                f"{GHL_API_BASE}/contacts/{next(iter(contact_ids))}", headers=headers,
            )
            contact_response.raise_for_status()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "GHL lookup failed stage=%s upstream_status=%s",
            lookup_stage,
            exc.response.status_code,
        )
        raise HTTPException(502, "GoHighLevel association lookup failed; no portal data was changed.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "GHL lookup failed stage=%s failure_type=%s",
            lookup_stage,
            type(exc).__name__,
        )
        raise HTTPException(502, "GoHighLevel association lookup failed; no portal data was changed.") from exc

    payload = contact_response.json()
    contact = payload.get("contact", payload) if isinstance(payload, dict) else {}
    email = str(contact.get("email", "")).strip().lower()
    if email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
        raise HTTPException(409, "The associated GoHighLevel Contact needs a valid email before syncing.")
    return email


def _line_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
    if "cargo" in value:
        return "cargo"
    if "general liability" in value or value in {"gl", "cgl"}:
        return "gl"
    if "physical damage" in value or value in {"apd", "comp collision"}:
        return "physical_damage"
    if "auto" in value or "trucking" in value:
        return "auto"
    return value.replace(" ", "_")


def _line_label(line: str) -> str:
    return {
        "auto": "Auto Liability",
        "cargo": "Motor Truck Cargo",
        "gl": "Commercial General Liability",
        "physical_damage": "Automobile Physical Damage",
    }.get(line, line.replace("_", " ").title())


def _carrier_row(body: PolicyLineSyncRequest, line: str) -> dict:
    try:
        naic = _naic_for(body.carrier, body.carrier_naic)
    except CarrierIdentityError as exc:
        raise HTTPException(422, detail={
            "status": "refused",
            "message": "Use the exact underwriting company from the declarations page, not a carrier brand.",
            "detail": str(exc),
        })
    if not naic:
        raise HTTPException(422, "Carrier NAIC is required for this underwriting company.")

    if line == "auto":
        limits = {"CSL": body.coverage_limit}
    elif line == "cargo":
        limits = {"Limit": body.coverage_limit}
    elif line == "gl":
        limits = {
            "EACH OCCURRENCE": body.coverage_limit,
            "GENERAL AGGREGATE": body.aggregate_limit,
        }
    else:
        limits = {"Limit": body.coverage_limit} if body.coverage_limit else {}

    row = {
        "ghl_policy_id": body.ghl_policy_id.strip(),
        "line": _line_label(line),
        "carrier": body.carrier.strip(),
        "naic": str(naic),
        "policy_number": body.policy_number.strip(),
        "eff": body.effective_date.strftime("%m/%d/%Y"),
        "exp": body.expiration_date.strftime("%m/%d/%Y"),
        "effective_date": body.effective_date.isoformat(),
        "expiration_date": body.expiration_date.isoformat(),
        "limits": limits,
    }
    if line == "auto":
        row["autos"] = [str(v).strip().upper() for v in body.auto_symbols if str(v).strip()]
    if line == "gl":
        form = re.sub(r"[^a-z]", "", body.gl_coverage_form.lower())
        if form not in {"occurrence", "claimsmade"}:
            raise HTTPException(422, "GL Coverage Form must be Occurrence or Claims Made.")
        aggregate = re.sub(r"[^a-z]", "", body.gl_aggregate_basis.lower())
        aggregate_aliases = {"policy": "POLICY", "project": "PROJECT", "location": "LOC", "loc": "LOC"}
        if aggregate not in aggregate_aliases:
            raise HTTPException(422, "GL Aggregate Basis must be Policy, Project, or Location.")
        row["form"] = "CLAIMS-MADE" if form == "claimsmade" else "OCCURRENCE"
        row["aggregate"] = aggregate_aliases[aggregate]
    if body.deductible is not None:
        row["deductible"] = body.deductible
    return row


def _combined_dates(carriers: list[dict], fallback: PolicyLineSyncRequest) -> tuple[str, str]:
    starts, ends = [], []
    for carrier in carriers:
        try:
            starts.append(dt.date.fromisoformat(carrier["effective_date"]))
            ends.append(dt.date.fromisoformat(carrier["expiration_date"]))
        except (KeyError, TypeError, ValueError):
            pass
    return (
        (max(starts) if starts else fallback.effective_date).isoformat(),
        (min(ends) if ends else fallback.expiration_date).isoformat(),
    )


def _coverages(carriers: list[dict]) -> dict:
    result = {}
    for carrier in carriers:
        line = _line_key(carrier.get("line", ""))
        limits = carrier.get("limits") or {}
        amount = limits.get("CSL") if line == "auto" else next(iter(limits.values()), None)
        if amount:
            result[{
                "auto": "auto_liability", "cargo": "cargo",
                "gl": "general_liability", "physical_damage": "physical_damage",
            }.get(line, line)] = amount
    return result


def register(app):
    import main as m

    @app.post("/api/integrations/ghl/policies")
    async def sync_ghl_policy(
        body: PolicyLineSyncRequest,
        authorization: Optional[str] = Header(None),
    ):
        _verify_secret(authorization)

        client_email = await _resolve_associated_contact_email(body.ghl_policy_id.strip())
        clients = await m.sb_get("clients", {
            "email": f"eq.{client_email}", "select": "id,email", "limit": "2",
        })
        if len(clients) != 1:
            raise HTTPException(409, "Exactly one portal customer must match the associated Contact email before syncing.")
        client_id = clients[0]["id"]

        if body.portal_policy_id:
            matches = await m.sb_get("policies", {
                "id": f"eq.{body.portal_policy_id}", "select": "*", "limit": "1",
            })
            if matches and matches[0]["client_id"] != client_id:
                raise HTTPException(409, "The requested portal policy belongs to a different customer.")
            existing = matches[0] if matches else None
            policy_id = str(body.portal_policy_id)
        else:
            matches = await m.sb_get("policies", {
                "client_id": f"eq.{client_id}", "select": "*", "limit": "3",
            })
            if len(matches) > 1:
                raise HTTPException(409, "This customer has multiple portal policies; send portal_policy_id to select one.")
            existing = matches[0] if matches else None
            policy_id = existing["id"] if existing else str(
                uuid.uuid5(GHL_POLICY_NAMESPACE, f"client:{client_id}")
            )

        line = _line_key(body.line_of_business)
        carriers = [dict(c) for c in (existing or {}).get("carriers", [])]
        carriers = [c for c in carriers if not (
            c.get("ghl_policy_id") == body.ghl_policy_id
            or (_line_key(c.get("line", "")) == line
                and c.get("policy_number") == body.policy_number)
        )]
        if body.status == "active":
            # Only one active record per line. A newly activated renewal replaces
            # the prior term; pending renewals leave the current term untouched.
            carriers = [c for c in carriers if _line_key(c.get("line", "")) != line]
            carriers.append(_carrier_row(body, line))

        effective_date, expiration_date = _combined_dates(carriers, body)
        coverages = _coverages(carriers)
        has_auto = bool(coverages.get("auto_liability"))
        status = "active" if has_auto else (body.status if line == "auto" else (existing or {}).get("status", "pending"))
        # The Auto record is authoritative for the combined account's portal
        # switch. An unchecked Cargo/GL record must not turn off valid Auto.
        existing_self_serve = bool((existing or {}).get("self_serve_enabled", False))
        requested_self_serve = (
            body.self_serve_enabled
            if line == "auto" and body.self_serve_enabled is not None
            else existing_self_serve
        )
        producer_block = body.producer_block or (existing or {}).get("producer_block")
        if not producer_block:
            raise HTTPException(422, "producer_block is required for the first portal policy sync.")

        row = {
            "id": policy_id,
            "client_id": client_id,
            "named_insured": body.named_insured.strip(),
            "usdot": body.usdot,
            "status": status,
            "self_serve_enabled": bool(requested_self_serve and status == "active" and has_auto),
            "effective_date": effective_date,
            "expiration_date": expiration_date,
            "producer_block": producer_block,
            "insured_address": body.insured_address.strip(),
            "coverages": coverages,
            "carriers": carriers,
            "vehicles": (existing or {}).get("vehicles", []),
            "trailers": (existing or {}).get("trailers", []),
            "drivers": (existing or {}).get("drivers", []),
            "data_current_as_of": body.data_current_as_of.isoformat(),
        }
        synced = await m.sb_upsert("policies", row)
        return {
            "status": "synced",
            "action": "updated" if existing else "created",
            "policy_id": synced.get("id", policy_id),
            "line": _line_label(line),
            "active_lines": len(carriers),
            "self_serve_enabled": row["self_serve_enabled"],
        }
