"""Handover between the old hall and the new branches.

The two systems keep their own databases.  Nothing here opens a transaction
spanning both; instead every cross-system step is an idempotent request that can
be retried after a crash:

* new-hall issuance reconciles against the old hall *before* drawing a seat,
  and imports the old hall's ticket so the same person is not counted twice;
* the old hall imports a ticket the new hall issued, so a late retry with an old
  request id is refused a second seat;
* the old hall reserves by stable request id with a durable pending marker, so
  a lost reply recovers exactly the original ticket number.

A branch can be closed briefly for its own handover while other branches stay
open and keep issuing.
"""

from __future__ import annotations

from typing import Any

from .legacy import LegacyHall
from .reservation_store import ReservationStore


class Handover:
    def __init__(
        self, reservation: ReservationStore, legacy: LegacyHall | None = None
    ) -> None:
        self.reservation = reservation
        self.legacy = legacy

    # ---------------------------------------------------------------- bridges
    def issue_at_new_hall(
        self,
        *,
        request_id: str,
        window_id: str,
        service_day: str,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Issue from a new window, reconciling a possibly-lost old ticket."""

        def reconcile(person: str, day: str) -> dict[str, Any] | None:
            if self.legacy is None:
                return None
            return self.legacy.locate_person(person, day)

        return self.reservation.issue(
            request_id=request_id,
            window_id=window_id,
            service_day=service_day,
            ticket_id=ticket_id,
            reconcile=reconcile,
        )

    def old_hall_reserve(
        self,
        *,
        request_id: str,
        window_id: str,
        hall_id: str,
        service_day: str,
        ticket_id: str,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        """Reserve at the old hall and mirror an issued seat into the new ledger.

        The mirror is best-effort and idempotent; if it fails, the old hall is
        still correct and a later lookup will import the ticket.
        """

        if self.legacy is None:
            raise RuntimeError("no legacy hall attached")
        result = self.legacy.reserve(
            request_id=request_id,
            window_id=window_id,
            hall_id=hall_id,
            service_day=service_day,
            ticket_id=ticket_id,
            capacity=capacity,
        )
        if result.get("status") == "issued":
            self.reservation.import_external_ticket(
                ticket_id=ticket_id,
                service_day=service_day,
                ticket_no=str(result["ticket_no"]),
                window_id=window_id,
                external_id=request_id,
                branch_id=hall_id,
                source="legacy",
            )
        return result

    def close_branch_for_handover(self, branch_id: str) -> dict[str, Any]:
        """Briefly stop issuing at one branch; other branches keep working."""

        return self.reservation.set_branch_open(branch_id, False)

    def reopen_branch(self, branch_id: str) -> dict[str, Any]:
        return self.reservation.set_branch_open(branch_id, True)

    # ----------------------------------------------------------- reporting
    def person_status(self, ticket_id: str, service_day: str) -> dict[str, Any]:
        """Where does this person stand today across both systems?"""

        new = self.reservation.has_ticket(ticket_id, service_day)
        old = (
            self.legacy.locate_person(ticket_id, service_day)
            if self.legacy is not None
            else None
        )
        return {
            "ticket_id": ticket_id,
            "service_day": service_day,
            "new_system": new,
            "old_system": old,
        }
