"""Listing lifecycle state machine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

ACTIONS = (
    "submit",
    "verify",
    "approve",
    "reject",
    "activate",
    "deactivate",
    "resubmit",
)

LEGACY_STATUS_ALIASES = {"verified": "approved"}


class InvalidTransitionError(ValueError):
    pass


class ListingState(ABC):
    value: ClassVar[str]
    visible_to_students: ClassVar[bool]
    messageable: ClassVar[bool]
    allowed_actions: ClassVar[frozenset[str]]

    def transition(self, action: str) -> ListingState:
        if action not in self.allowed_actions:
            raise InvalidTransitionError(
                f"invalid transition {action!r} from {self.value}"
            )
        return self._transition(action)

    @abstractmethod
    def _transition(self, action: str) -> ListingState:
        raise NotImplementedError


class PendingState(ListingState):
    value = "pending"
    visible_to_students = False
    messageable = False
    allowed_actions = frozenset({"submit", "verify", "approve", "reject"})

    def _transition(self, action: str) -> ListingState:
        return {
            "submit": PendingState,
            "verify": ApprovedState,
            "approve": ApprovedState,
            "reject": RejectedState,
        }[action]()


class ApprovedState(ListingState):
    value = "approved"
    visible_to_students = False
    messageable = False
    allowed_actions = frozenset({"activate", "reject"})

    def _transition(self, action: str) -> ListingState:
        return {
            "activate": ActiveState,
            "reject": RejectedState,
        }[action]()


class RejectedState(ListingState):
    value = "rejected"
    visible_to_students = False
    messageable = False
    allowed_actions = frozenset({"resubmit"})

    def _transition(self, action: str) -> ListingState:
        return {"resubmit": PendingState}[action]()


class ActiveState(ListingState):
    value = "active"
    visible_to_students = True
    messageable = True
    allowed_actions = frozenset({"deactivate"})

    def _transition(self, action: str) -> ListingState:
        return {"deactivate": InactiveState}[action]()


class InactiveState(ListingState):
    value = "inactive"
    visible_to_students = False
    messageable = False
    allowed_actions = frozenset({"activate"})

    def _transition(self, action: str) -> ListingState:
        return {"activate": ActiveState}[action]()


def normalize_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    return LEGACY_STATUS_ALIASES.get(value, value)


def state_for(status: Any) -> ListingState:
    normalized = normalize_status(status)
    states: dict[str, type[ListingState]] = {
        PendingState.value: PendingState,
        ApprovedState.value: ApprovedState,
        RejectedState.value: RejectedState,
        ActiveState.value: ActiveState,
        InactiveState.value: InactiveState,
    }
    state_class = states.get(normalized)
    if state_class is None:
        raise ValueError(f"unknown listing status {status!r}")
    return state_class()


def flags_for(state: ListingState) -> dict[str, int]:
    return {
        "flag_pending": int(isinstance(state, PendingState)),
        "flag_verified": int(isinstance(state, ApprovedState)),
        "flag_active": int(isinstance(state, ActiveState)),
        "flag_rejected": int(isinstance(state, RejectedState)),
    }


def fields_for_status(status: Any) -> dict[str, Any]:
    state = state_for(status)
    return {"status": state.value, **flags_for(state)}


def snapshot(listing: dict[str, Any]) -> dict[str, Any]:
    state = state_for(listing.get("status"))
    derived_flags = {key: bool(value) for key, value in flags_for(state).items()}
    return {
        "id": listing.get("id"),
        "status": state.value,
        **derived_flags,
    }


def _ok(
    listing: dict[str, Any],
    next_state: ListingState,
    *,
    changed: bool,
) -> dict[str, Any]:
    updated = dict(listing)
    updated["status"] = next_state.value
    updated.update(flags_for(next_state))
    return {
        "ok": True,
        "listing": snapshot(updated),
        "row": updated,
        "changed": changed,
    }


def apply_action(listing: dict[str, Any], action: str) -> dict[str, Any]:
    if action not in ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}"}

    try:
        current = state_for(listing.get("status"))
        next_state = current.transition(action)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    return _ok(listing, next_state, changed=current.value != next_state.value)


def visible_to_students(listing: dict[str, Any]) -> bool:
    try:
        return state_for(listing.get("status")).visible_to_students
    except ValueError:
        return False


def messageable(listing: dict[str, Any]) -> bool:
    try:
        return state_for(listing.get("status")).messageable
    except ValueError:
        return False
