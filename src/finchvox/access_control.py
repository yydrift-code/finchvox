import json
import os
import re
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from finchvox.session import Session


ROLE_HEADER = "X-Finchvox-Role"
TENANT_HEADER = "X-Finchvox-Tenant"
_TENANT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?$")


def _is_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_tenant_id(value: str | None) -> str | None:
    if value is None:
        return None

    tenant_id = value.strip().lower().rstrip(".")
    if not tenant_id or not _TENANT_ID_PATTERN.fullmatch(tenant_id):
        return None
    return tenant_id


@dataclass(frozen=True)
class AccessScope:
    role: str
    tenant_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_view_diagnostics(self) -> bool:
        return self.is_admin

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "tenant_id": self.tenant_id,
            "can_view_diagnostics": self.can_view_diagnostics,
            "can_upload_sessions": self.is_admin,
        }


@dataclass(frozen=True)
class AccessControlPolicy:
    enabled: bool = False
    legacy_tenant_source_map: dict[str, str] = field(default_factory=dict)
    tenant_excluded_owners: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "AccessControlPolicy":
        enabled = _is_enabled(os.environ.get("FINCHVOX_ACCESS_CONTROL_ENABLED"))
        raw_source_map = os.environ.get("FINCHVOX_LEGACY_TENANT_SOURCE_MAP", "{}")
        raw_excluded_owners = os.environ.get("FINCHVOX_TENANT_EXCLUDED_OWNERS", "{}")

        try:
            parsed_source_map = json.loads(raw_source_map)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "FINCHVOX_LEGACY_TENANT_SOURCE_MAP must be a JSON object"
            ) from exc

        if not isinstance(parsed_source_map, dict):
            raise ValueError("FINCHVOX_LEGACY_TENANT_SOURCE_MAP must be a JSON object")

        source_map = {}
        for source, tenant_id in parsed_source_map.items():
            if not isinstance(source, str) or not isinstance(tenant_id, str):
                raise ValueError(
                    "FINCHVOX_LEGACY_TENANT_SOURCE_MAP keys and values must be strings"
                )
            normalized_tenant_id = normalize_tenant_id(tenant_id)
            if normalized_tenant_id is None:
                raise ValueError(
                    f"Invalid tenant ID in FINCHVOX_LEGACY_TENANT_SOURCE_MAP: {tenant_id}"
                )
            source_map[source] = normalized_tenant_id

        try:
            parsed_excluded_owners = json.loads(raw_excluded_owners)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "FINCHVOX_TENANT_EXCLUDED_OWNERS must be a JSON object"
            ) from exc
        if not isinstance(parsed_excluded_owners, dict):
            raise ValueError("FINCHVOX_TENANT_EXCLUDED_OWNERS must be a JSON object")

        excluded_owners = {}
        for tenant_id, owners in parsed_excluded_owners.items():
            normalized_tenant_id = (
                normalize_tenant_id(tenant_id) if isinstance(tenant_id, str) else None
            )
            if (
                normalized_tenant_id is None
                or not isinstance(owners, list)
                or any(not isinstance(owner, str) or not owner for owner in owners)
            ):
                raise ValueError("FINCHVOX_TENANT_EXCLUDED_OWNERS has invalid entries")
            excluded_owners[normalized_tenant_id] = frozenset(
                owner.strip().lower() for owner in owners
            )

        return cls(
            enabled=enabled,
            legacy_tenant_source_map=source_map,
            tenant_excluded_owners=excluded_owners,
        )

    def scope_for_request(self, request: Request) -> AccessScope:
        if not self.enabled:
            return AccessScope(role="admin")

        role = (request.headers.get(ROLE_HEADER) or "").strip().lower()
        if role == "admin":
            return AccessScope(role="admin")

        if role == "tenant":
            tenant_id = normalize_tenant_id(request.headers.get(TENANT_HEADER))
            if tenant_id is not None:
                return AccessScope(role="tenant", tenant_id=tenant_id)

        raise HTTPException(status_code=403, detail="FinchVox access scope is invalid")

    def tenant_for_session(self, session: Session) -> str:
        if session.explicit_tenant_id:
            return normalize_tenant_id(session.explicit_tenant_id) or "unknown"
        if session.session_source:
            mapped = self.legacy_tenant_source_map.get(session.session_source)
            if mapped:
                return mapped
        return session.tenant_id

    def can_view(self, scope: AccessScope, session: Session) -> bool:
        if scope.is_admin:
            return True
        owner = self.tenant_for_session(session)
        excluded = self.tenant_excluded_owners.get(scope.tenant_id)
        if excluded is not None:
            return owner.lower() not in excluded
        return owner == scope.tenant_id

    def require_session_access(self, scope: AccessScope, session: Session) -> None:
        if not self.can_view(scope, session):
            # Do not disclose whether another tenant's session exists.
            raise HTTPException(status_code=404, detail="Session not found")

    def require_admin(self, scope: AccessScope) -> None:
        if not scope.is_admin:
            raise HTTPException(status_code=403, detail="Administrator access required")

    def require_diagnostics_access(self, scope: AccessScope) -> None:
        if not scope.can_view_diagnostics:
            raise HTTPException(
                status_code=403, detail="Diagnostic access is restricted"
            )
