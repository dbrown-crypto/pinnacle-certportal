"""Authenticated, idempotent GoHighLevel -> portal policy synchronization.

GoHighLevel remains the policy-data source of truth. This endpoint only writes
policy snapshots; it never issues a certificate, sends email, or bypasses the
portal's normal issuance gate.
"""

from __future__ import annotations

import datetime as dt
import hmac
import os
import uuid
from typing import Literal, Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from acord25_2016_overlay import CarrierIdentityError, _naic_for


# A fixed application namespace makes a GHL record ID resolve to the same
# portal UUID on every retry without storing the external ID in the database.
GHL_POLICY_NAMESPACE = uuid.UUID("91ae37d2-1ca5-4a58-bd2f-a213f65e9643")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DriverSync(StrictModel):
    """Intentionally excludes DOB and driver-license number."""

    first: str
    last: str
    lic_state: Optional[str] = None


class PolicySyncRequest(StrictModel):
    ghl_policy_id: str = Field(min_length=1, max_length=200)
    client_email: str = Field(min_length=3, max_length=320)
    portal_policy_id: Optional[uuid.UUID] = None
    named_insured: str = Field(min_length=1, max_length=300)
    usdot: Optional[str] = Field(default=None, max_length=30)
    status: Literal["active", "cancelled", "canceled", "pending", "expired"] = "active"
    self_serve_enabled: bool = False
    effective_date: dt.date
    expiration_date: dt.date
    producer_block: str = Field(min_length=1, max_length=1000)
    insured_address: str = Field(min_length=1, max_length=1000)
    coverages: dict = Field(default_factory=dict)
    carriers: list[dict] = Field(default_factory=list)
    vehicles: list[dict] = Field(default_factory=list)
    trailers: list[dict] = Field(default_factory=list)
    drivers: list[DriverSync] = Field(default_factory=list)
    # Required because the issuance gate uses this date to reject stale data.
    data_current_as_of: dt.date

    @field_validator("client_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        email = value.strip().lower()
        if email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
            raise ValueError("client_email must be a valid email address")
        return email

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.expiration_date < self.effective_date:
            raise ValueError("expiration_date cannot be before effective_date")
        return self


def _verify_secret(authorization: Optional[str]) -> None:
    expected = os.environ.get("GHL_POLICY_SYNC_SECRET", "")
    if len(expected) < 32:
        raise HTTPException(503, "Policy sync is not configured.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing policy sync bearer token.")
    supplied = authorization.split(" ", 1)[1]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid policy sync bearer token.")


def _portal_policy_id(body: PolicySyncRequest) -> str:
    if body.portal_policy_id:
        return str(body.portal_policy_id)
    return str(uuid.uuid5(GHL_POLICY_NAMESPACE, body.ghl_policy_id.strip()))


def _correct_carriers(carriers: list[dict]) -> list[dict]:
    corrected_carriers = []
    for carrier in carriers:
        corrected = dict(carrier)
        try:
            corrected["naic"] = _naic_for(
                corrected.get("carrier"), corrected.get("naic")
            )
        except CarrierIdentityError as exc:
            raise HTTPException(422, detail={
                "status": "refused",
                "message": "Use the exact underwriting company from the declarations page, not a carrier brand.",
                "detail": str(exc),
            })
        corrected_carriers.append(corrected)
    return corrected_carriers


def register(app):
    import main as m  # late import: register() runs after main's module body

    @app.post("/api/integrations/ghl/policies")
    async def sync_ghl_policy(
        body: PolicySyncRequest,
        authorization: Optional[str] = Header(None),
    ):
        _verify_secret(authorization)

        clients = await m.sb_get("clients", {
            "email": f"eq.{body.client_email}",
            "select": "id,email",
            "limit": "2",
        })
        if len(clients) != 1:
            raise HTTPException(
                409,
                "Exactly one portal customer must match client_email before syncing a policy.",
            )

        policy_id = _portal_policy_id(body)
        existing = await m.sb_get("policies", {
            "id": f"eq.{policy_id}",
            "select": "id,client_id",
            "limit": "1",
        })
        if existing and existing[0]["client_id"] != clients[0]["id"]:
            raise HTTPException(
                409,
                "This GHL policy is already linked to a different portal customer.",
            )

        status = "cancelled" if body.status == "canceled" else body.status
        row = body.model_dump(
            mode="json",
            exclude={"ghl_policy_id", "client_email", "portal_policy_id"},
        )
        row.update({
            "id": policy_id,
            "client_id": clients[0]["id"],
            "status": status,
            # A non-active policy can never remain self-serve after a sync.
            "self_serve_enabled": body.self_serve_enabled if status == "active" else False,
            "carriers": _correct_carriers(body.carriers),
        })
        row["drivers"] = [driver.model_dump(mode="json") for driver in body.drivers]

        synced = await m.sb_upsert("policies", row)
        return {
            "status": "synced",
            "action": "updated" if existing else "created",
            "policy_id": synced.get("id", policy_id),
            "self_serve_enabled": row["self_serve_enabled"],
        }
