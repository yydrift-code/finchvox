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
    legacy_tenant_service_map: dict[str, str] = field(default_factory=dict)
    legacy_tenant_service_contains_map: dict[str, str] = field(default_factory=dict)
    legacy_tenant_service_excludes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AccessControlPolicy":
        enabled = _is_enabled(os.environ.get("FINCHVOX_ACCESS_CONTROL_ENABLED"))
        raw_source_map = os.environ.get("FINCHVOX_LEGACY_TENANT_SOURCE_MAP", "{}")
        raw_service_map = os.environ.get("FINCHVOX_LEGACY_TENANT_SERVICE_MAP", "{}")
        raw_contains_map = os.environ.get(
            "FINCHVOX_LEGACY_TENANT_SERVICE_CONTAINS_MAP", "{}"
        )
        raw_excludes = os.environ.get("FINCHVOX_LEGACY_TENANT_SERVICE_EXCLUDES", "[]")

        def parse_tenant_map(raw: str, name: str) -> dict[str, str]:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} must be a JSON object") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"{name} must be a JSON object")

            result = {}
            for key, tenant_id in parsed.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or not isinstance(tenant_id, str)
                ):
                    raise ValueError(f"{name} keys and values must be strings")
                normalized_tenant_id = normalize_tenant_id(tenant_id)
                if normalized_tenant_id is None:
                    raise ValueError(f"Invalid tenant ID in {name}: {tenant_id}")
                result[key] = normalized_tenant_id
            return result

        try:
            parsed_excludes = json.loads(raw_excludes)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "FINCHVOX_LEGACY_TENANT_SERVICE_EXCLUDES must be a JSON array"
            ) from exc
        if not isinstance(parsed_excludes, list) or any(
            not isinstance(value, str) or not value for value in parsed_excludes
        ):
            raise ValueError(
                "FINCHVOX_LEGACY_TENANT_SERVICE_EXCLUDES must be an array of nonempty strings"
            )

        return cls(
            enabled=enabled,
            legacy_tenant_source_map=parse_tenant_map(
                raw_source_map, "FINCHVOX_LEGACY_TENANT_SOURCE_MAP"
            ),
            legacy_tenant_service_map=parse_tenant_map(
                raw_service_map, "FINCHVOX_LEGACY_TENANT_SERVICE_MAP"
            ),
            legacy_tenant_service_contains_map=parse_tenant_map(
                raw_contains_map, "FINCHVOX_LEGACY_TENANT_SERVICE_CONTAINS_MAP"
            ),
            legacy_tenant_service_excludes=tuple(
                value.lower() for value in parsed_excludes
            ),
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

    def tenant_for_session(self, session: Session) -> str | None:
        if session.tenant_id:
            return normalize_tenant_id(session.tenant_id)
        if session.session_source:
            mapped = self.legacy_tenant_source_map.get(session.session_source)
            if mapped:
                return mapped
        if session.service_name:
            mapped = self.legacy_tenant_service_map.get(session.service_name)
            if mapped:
                return mapped
            service_name = session.service_name.lower()
            if not any(
                excluded in service_name
                for excluded in self.legacy_tenant_service_excludes
            ):
                for (
                    substring,
                    tenant_id,
                ) in self.legacy_tenant_service_contains_map.items():
                    if substring.lower() in service_name:
                        return tenant_id
        return None

    def can_view(self, scope: AccessScope, session: Session) -> bool:
        if scope.is_admin:
            return True
        return self.tenant_for_session(session) == scope.tenant_id

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
