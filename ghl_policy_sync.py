"""Authenticated, idempotent GoHighLevel -> portal policy-line sync.

GHL stores Auto, Cargo and GL as separate Policy custom-object records. The
portal stores one combined snapshot per customer. This module merges each GHL
line into that snapshot; it never issues a certificate or creates a customer.
"""

from __future__ import annotations

import datetime as dt
import hmac
import os
import re
import uuid
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from acord25_2016_overlay import CarrierIdentityError, _naic_for


GHL_POLICY_NAMESPACE = uuid.UUID("91ae37d2-1ca5-4a58-bd2f-a213f65e9643")


class PolicyLineSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ghl_policy_id: str = Field(min_length=1, max_length=200)
    client_email: str = Field(min_length=3, max_length=320)
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
    auto_symbols: list[str] = Field(default_factory=lambda: ["SCHEDULED"])

    @field_validator("client_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().lower()
        if email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
            raise ValueError("client_email must be a valid email address")
        return email

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z]", "", value.lower())
        aliases = {
            "active": "active", "inforce": "active", "inforced": "active",
            "cancelled": "cancelled", "canceled": "cancelled",
            "pending": "pending", "expired": "expired",
        }
        if normalized not in aliases:
            raise ValueError("status must be Active, In Force, Pending, Cancelled, or Expired")
        return aliases[normalized]

    @field_validator("carrier_naic", "usdot", "producer_block", mode="before")
    @classmethod
    def blank_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("coverage_limit", "aggregate_limit", "deductible", mode="before")
    @classmethod
    def blank_number_to_none(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value.replace("$", "").replace(",", "").strip()
        return value

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
        return self


def _verify_secret(authorization: Optional[str]) -> None:
    expected = os.environ.get("GHL_POLICY_SYNC_SECRET", "")
    if len(expected) < 32:
        raise HTTPException(503, "Policy sync is not configured.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing policy sync bearer token.")
    if not hmac.compare_digest(authorization.split(" ", 1)[1], expected):
        raise HTTPException(401, "Invalid policy sync bearer token.")


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

        clients = await m.sb_get("clients", {
            "email": f"eq.{body.client_email}", "select": "id,email", "limit": "2",
        })
        if len(clients) != 1:
            raise HTTPException(409, "Exactly one portal customer must match client_email before syncing a policy.")
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
