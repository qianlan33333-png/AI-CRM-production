from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SaveSidebarProfileFieldsRequest:
    unionid: str
    source: str
    industry: str
    industry_description: str
    needs_blockers_followup: str
    updated_by: str
    customer_id: int = 0


class SidebarCustomerProfileProjectionPort(Protocol):
    """Public write port for the sidebar-only customer profile projection."""

    def save_fields_dbapi(
        self,
        executor: Any,
        *,
        request: SaveSidebarProfileFieldsRequest,
    ) -> dict[str, Any]: ...


def build_sidebar_customer_profile_projection_port() -> SidebarCustomerProfileProjectionPort:
    from .sidebar_profile_repository import PostgresSidebarCustomerProfileProjectionRepository

    return PostgresSidebarCustomerProfileProjectionRepository()


__all__ = [
    "SaveSidebarProfileFieldsRequest",
    "SidebarCustomerProfileProjectionPort",
    "build_sidebar_customer_profile_projection_port",
]
