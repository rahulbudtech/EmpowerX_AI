"""Pydantic models for verified EmpowerX access tokens."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AccessTokenClaims(BaseModel):
    """
    Claims from a verified Node access JWT.

    Field names match Node (camelCase in the token). Specialist tokens omit
    ``facilityId`` / ``role``; facility/technician tokens omit ``specialistId``.
    """

    model_config = ConfigDict(extra="ignore")

    sub: str
    type: str
    email: str | None = None
    role: str | None = None
    facility_id: str | None = Field(default=None, alias="facilityId")
    specialist_id: str | None = Field(default=None, alias="specialistId")

    @property
    def user_id(self) -> str:
        return self.sub
