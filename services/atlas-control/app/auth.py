import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status


@dataclass(frozen=True)
class Principal:
    subject_id: str
    tenant_id: UUID
    permissions: frozenset[str]
    principal_type: str


class IdentityProvider(Protocol):
    def authenticate(self, bearer_token: str) -> Principal | None:
        """Return a trusted principal for a valid bearer token."""


DEFAULT_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")


@lru_cache(maxsize=1)
def development_principals() -> dict[str, Principal]:
    tenant_id = UUID(os.getenv("ATLAS_DEV_TENANT_ID", str(DEFAULT_TENANT_ID)))

    return {
        "dev-requester": Principal(
            subject_id=os.getenv("ATLAS_DEV_REQUESTER_ID", "atlas-requester"),
            tenant_id=tenant_id,
            permissions=frozenset(
                {
                    "approval-gates:create",
                    "approval-gates:read",
                    "jobs:create",
                    "jobs:read",
                    "research:intake:create",
                    "research:intake:read",
                    "research:evidence:create",
                    "research:evidence:read",
                }
            ),
            principal_type="user",
        ),
        "dev-approver": Principal(
            subject_id=os.getenv("ATLAS_DEV_APPROVER_ID", "atlas-approver"),
            tenant_id=tenant_id,
            permissions=frozenset(
                {
                    "approval-gates:read",
                    "approval-gates:decide",
                    "jobs:read",
                }
            ),
            principal_type="user",
        ),
        "dev-worker": Principal(
            subject_id=os.getenv("ATLAS_DEV_WORKER_ID", "atlas-job-worker"),
            tenant_id=tenant_id,
            permissions=frozenset(
                {
                    "jobs:execute",
                    "jobs:read",
                }
            ),
            principal_type="service",
        ),
        "dev-admin": Principal(
            subject_id=os.getenv("ATLAS_DEV_ADMIN_ID", "atlas-admin"),
            tenant_id=tenant_id,
            permissions=frozenset(
                {
                    "approval-gates:create",
                    "approval-gates:read",
                    "approval-gates:decide",
                    "jobs:create",
                    "jobs:read",
                    "jobs:execute",
                    "research:intake:create",
                    "research:intake:read",
                    "research:evidence:create",
                    "research:evidence:read",
                    "platform:admin",
                }
            ),
            principal_type="user",
        ),
    }


class DevelopmentIdentityProvider:
    def authenticate(self, bearer_token: str) -> Principal | None:
        return development_principals().get(bearer_token)


def identity_provider() -> IdentityProvider:
    return DevelopmentIdentityProvider()


async def get_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing authorization credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = identity_provider().authenticate(token.strip())
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid authorization credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return principal


def require_permission(permission: str):
    async def authorized_dependency(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="permission denied",
            )
        return principal

    return authorized_dependency
