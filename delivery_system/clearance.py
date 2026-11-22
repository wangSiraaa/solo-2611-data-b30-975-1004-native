"""Clearance service: from a frozen translation package to signed tickets.

The entry points consume an actual ``translation-package/1`` package.  Block
identity comes straight from each frozen chunk (strip id + every source
revision), so a chunk that merely shares text with another strip can never be
cleared against that other strip's registration.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .rights_engine import RightsEngine
from .rights_models import BlockRef, SigningAnswer, Ticket, TicketPlan


def blocks_from_package(content: dict[str, Any]) -> list[BlockRef]:
    """Extract the actual translation blocks of a frozen package, in order."""

    return [BlockRef.from_chunk(chunk) for chunk in content["chunks"]]


def package_language(content: dict[str, Any]) -> str:
    return str(content["language"])


def package_document(content: dict[str, Any]) -> str:
    return str(content["document_id"])


def screen_block(
    engine: RightsEngine,
    content: dict[str, Any],
    block: BlockRef,
    right: str,
    vendor: str,
    *,
    venue: str,
    day: str,
    purpose: Optional[str] = None,
) -> SigningAnswer:
    """Answer one rights-desk question for an actual block of a package."""

    return engine.can_sign(
        vendor,
        package_document(content),
        block,
        right,
        venue=venue,
        language=package_language(content),
        day=day,
        purpose=purpose,
    )


def plan_screening(
    engine: RightsEngine,
    content: dict[str, Any],
    *,
    venue: str,
    days: Sequence[str],
    tickets: Sequence[Ticket],
    purpose: Optional[str] = None,
) -> TicketPlan:
    """Minimum-fee ticket plan covering every block/right/day of a package."""

    return engine.plan_tickets(
        package_document(content),
        blocks_from_package(content),
        venue=venue,
        language=package_language(content),
        days=days,
        tickets=tickets,
        purpose=purpose,
    )
