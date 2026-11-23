import pytest

from app.services.lifecycle import (
    ACTIONS,
    apply_action,
    fields_for_status,
    messageable,
    normalize_status,
    snapshot,
    state_for,
    visible_to_students,
)

FLAG_KEYS = (
    "flag_pending",
    "flag_verified",
    "flag_active",
    "flag_rejected",
)


def _row(status, **overrides):
    row = {
        "id": 1,
        "status": status,
        "flag_pending": 0,
        "flag_verified": 0,
        "flag_active": 0,
        "flag_rejected": 0,
    }
    row.update(overrides)
    return row


def _assert_result(result, status, flags):
    assert result["ok"] is True
    assert result["row"]["status"] == status
    assert result["listing"]["status"] == status
    assert tuple(result["row"][key] for key in FLAG_KEYS) == flags
    assert tuple(result["listing"][key] for key in FLAG_KEYS) == tuple(
        bool(flag) for flag in flags
    )


def test_pending_to_approved_via_verify():
    result = apply_action(_row("pending"), "verify")

    _assert_result(result, "approved", (0, 1, 0, 0))
    assert result["changed"] is True


def test_pending_to_approved_via_approve():
    assert "approve" in ACTIONS

    result = apply_action(_row("pending"), "approve")

    _assert_result(result, "approved", (0, 1, 0, 0))
    assert result["changed"] is True


def test_pending_to_rejected():
    result = apply_action(_row("pending"), "reject")

    _assert_result(result, "rejected", (0, 0, 0, 1))
    assert result["changed"] is True


def test_approved_to_active():
    result = apply_action(_row("approved"), "activate")

    _assert_result(result, "active", (0, 0, 1, 0))
    assert result["changed"] is True


def test_active_to_inactive():
    result = apply_action(_row("active"), "deactivate")

    _assert_result(result, "inactive", (0, 0, 0, 0))
    assert result["changed"] is True


def test_rejected_to_pending_resubmit():
    result = apply_action(_row("rejected"), "resubmit")

    _assert_result(result, "pending", (1, 0, 0, 0))
    assert result["changed"] is True


def test_submit_is_noop_only_from_pending():
    noop = apply_action(_row("pending"), "submit")

    _assert_result(noop, "pending", (1, 0, 0, 0))
    assert noop["changed"] is False

    invalid = apply_action(_row("active"), "submit")

    assert invalid["ok"] is False
    assert "invalid transition" in invalid["error"]


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("pending", "activate"),
        ("pending", "deactivate"),
        ("pending", "resubmit"),
        ("approved", "verify"),
        ("approved", "approve"),
        ("approved", "deactivate"),
        ("approved", "resubmit"),
        ("rejected", "verify"),
        ("rejected", "approve"),
        ("rejected", "activate"),
        ("rejected", "deactivate"),
        ("rejected", "reject"),
        ("rejected", "submit"),
        ("active", "verify"),
        ("active", "approve"),
        ("active", "activate"),
        ("active", "reject"),
        ("active", "resubmit"),
        ("active", "submit"),
        ("inactive", "verify"),
        ("inactive", "approve"),
        ("inactive", "deactivate"),
        ("inactive", "reject"),
        ("inactive", "resubmit"),
        ("inactive", "submit"),
    ],
)
def test_invalid_transitions(status, action):
    result = apply_action(_row(status), action)

    assert result["ok"] is False
    assert "invalid transition" in result["error"]


def test_unknown_action_returns_error():
    result = apply_action(_row("pending"), "destroy")

    assert result["ok"] is False
    assert "unknown action" in result["error"]


def test_verified_alias_maps_to_approved():
    assert normalize_status("verified") == "approved"
    assert state_for("verified").value == "approved"
    assert fields_for_status("verified") == {
        "status": "approved",
        "flag_pending": 0,
        "flag_verified": 1,
        "flag_active": 0,
        "flag_rejected": 0,
    }

    result = apply_action(_row("verified"), "activate")

    _assert_result(result, "active", (0, 0, 1, 0))


@pytest.mark.parametrize(
    ("status", "flags"),
    [
        ("pending", (True, False, False, False)),
        ("approved", (False, True, False, False)),
        ("verified", (False, True, False, False)),
        ("rejected", (False, False, False, True)),
        ("active", (False, False, True, False)),
        ("inactive", (False, False, False, False)),
    ],
)
def test_flags_are_derived_from_status(status, flags):
    listed = _row(
        status,
        flag_pending=1,
        flag_verified=1,
        flag_active=1,
        flag_rejected=1,
    )

    result = snapshot(listed)

    assert result["status"] == normalize_status(status)
    assert tuple(result[key] for key in FLAG_KEYS) == flags


def test_stale_active_flag_does_not_make_pending_visible():
    listed = _row("pending", flag_active=1)

    assert visible_to_students(listed) is False
    assert messageable(listed) is False


def test_reject_clears_stale_active_flag():
    result = apply_action(_row("pending", flag_active=1), "reject")

    _assert_result(result, "rejected", (0, 0, 0, 1))


def test_resubmit_clears_rejected_flag():
    listed = _row(
        "rejected",
        flag_rejected=1,
        flag_verified=1,
        flag_active=1,
    )

    result = apply_action(listed, "resubmit")

    _assert_result(result, "pending", (1, 0, 0, 0))
