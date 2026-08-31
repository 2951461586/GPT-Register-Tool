import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sms_tool import registration_concurrency
from sms_tool.registration_concurrency import RegistrationStageLease

STAGE_CFG = {"registration": {"stage_concurrency": {"auth": 1, "network": 2, "at_probe": 1, "cross_process": False}}}


def _gate_permits(semaphore: threading.BoundedSemaphore) -> int:
    """Remaining permits of a semaphore (CPython exposes it as ``_value``)."""
    return int(getattr(semaphore, "_value", -1))


def test_stage_groups_are_owned_by_concurrency_module():
    assert registration_concurrency.registration_stage_group("auth_flow") == "auth"
    assert registration_concurrency.registration_stage_group("access_token_probe") == "at_probe"
    assert registration_concurrency.registration_stage_group("payment_link") == "payment"
    assert registration_concurrency.registration_stage_group("completed") == ""


def test_ungated_stage_returns_no_lease():
    with patch.object(registration_concurrency, "CFG", STAGE_CFG):
        assert registration_concurrency.acquire_registration_stage("completed") is None


def test_stage_transitions_are_independently_leased_and_record_metrics():
    registration_concurrency.registration_stage_metrics(reset=True)
    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        leases = []
        try:
            for stage in ("auth_flow", "email_otp_send", "access_token_probe"):
                lease = registration_concurrency.acquire_registration_stage(stage)
                assert isinstance(lease, RegistrationStageLease)
                leases.append(lease)
            # Every stage owns its own lease; none of them is aliased.
            assert len({id(item) for item in leases}) == 3
            assert [item.group for item in leases] == ["auth", "network", "at_probe"]
            assert all(not item.released for item in leases)
        finally:
            for lease in leases:
                lease.release()
        assert all(item.released for item in leases)

    metrics = registration_concurrency.registration_stage_metrics(reset=True)
    assert metrics["auth"]["transitions"] == 1
    assert metrics["network"]["transitions"] == 1
    assert metrics["at_probe"]["transitions"] == 1
    assert all(not row.get("over_releases") for row in metrics.values())


def test_lease_release_is_idempotent_and_reports_over_release():
    registration_concurrency.registration_stage_metrics(reset=True)
    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        lease = registration_concurrency.acquire_registration_stage("email_otp_send")
        assert lease is not None
        lease.release()
        lease.release()  # idempotent, must not raise
        assert lease.released

        # A second, fully independent lease may hand the permit back too late;
        # the over-release must be recorded rather than silently swallowed.
        rogue = RegistrationStageLease("network", lease._gate, None, 0.0)
        rogue.release()
        rogue.release()

    metrics = registration_concurrency.registration_stage_metrics(reset=True)
    assert metrics["network"]["over_releases"] >= 1


def test_lease_supports_context_manager():
    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        gate = registration_concurrency._gate_for("auth")
        capacity = _gate_permits(gate)
        with registration_concurrency.acquire_registration_stage("auth_flow") as lease:
            assert _gate_permits(gate) == capacity - 1
            assert not lease.released
        assert lease.released
        assert _gate_permits(gate) == capacity


def test_rate_limit_circuit_blocks_new_auth_admission():
    registration_concurrency.clear_registration_rate_limit()
    try:
        registration_concurrency.mark_registration_rate_limited(60)
        with patch.object(registration_concurrency, "CFG", STAGE_CFG):
            try:
                registration_concurrency.acquire_registration_stage("auth_flow")
            except RuntimeError as exc:
                assert "registration_rate_limit_circuit_open" in str(exc)
            else:
                raise AssertionError("rate-limit circuit did not block auth admission")
    finally:
        registration_concurrency.clear_registration_rate_limit()


def test_unpaired_stage_lease_does_not_leak_into_next_threadpool_task():
    """A gate held by one pool task must not satisfy the next task.

    ``ThreadPoolExecutor`` reuses worker threads and ``submit()`` does not copy
    the context, so a ``ContextVar`` recording "which gate do I own" leaks into
    the following task on the same worker. That task then believes it already
    holds the group and **skips acquisition entirely**, silently disabling the
    cap. Reproduced on CPython 3.13 with both single and multiple workers; the
    previous context-local ownership failed this with ``[1, 1]``.
    """
    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        gate = registration_concurrency._gate_for("network")
        capacity = _gate_permits(gate)
        leaked: list[RegistrationStageLease] = []

        def task(index: int):
            if index == 0:
                # Deliberately leak: acquire without releasing. Any new call
                # site that forgets to release behaves like this.
                lease = registration_concurrency.acquire_registration_stage("email_otp_send")
                leaked.append(lease)
                return None
            lease = registration_concurrency.acquire_registration_stage("email_otp_send")
            held = _gate_permits(gate)
            lease.release()
            return held

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed = list(pool.map(task, range(3)))
        finally:
            for lease in leaked:
                lease.release()

    # Tasks 1 and 2 must each take a permit of their own, so the leaked permit
    # from task 0 plus their own leaves `capacity - 2`.
    assert observed[1:] == [capacity - 2, capacity - 2], (
        "stage gate leaked across pool tasks: a later task skipped acquisition "
        f"(permits seen {observed[1:]}, expected {[capacity - 2, capacity - 2]})"
    )


def test_unpaired_stage_lease_does_not_leak_with_multiple_workers():
    """Same guarantee as above when the pool interleaves tasks across workers."""
    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        gate = registration_concurrency._gate_for("network")
        capacity = _gate_permits(gate)
        leaked: list[RegistrationStageLease] = []

        def task(index: int):
            if index == 0:
                lease = registration_concurrency.acquire_registration_stage("email_otp_send")
                leaked.append(lease)
                return None
            lease = registration_concurrency.acquire_registration_stage("email_otp_send")
            held = _gate_permits(gate)
            lease.release()
            return held

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                observed = list(pool.map(task, range(4)))
        finally:
            for lease in leaked:
                lease.release()

    assert observed[1:] == [capacity - 2] * 3, f"permits seen {observed[1:]}"


def test_progress_object_owns_its_gate_and_releases_on_track_exit():
    """``RegistrationProgress`` must own the lease, not a context-local slot."""
    from sms_tool import registration_progress

    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ), patch.object(registration_progress, "_current") as current:
        progress = registration_progress.RegistrationProgress("a@example.com")
        current.get.return_value = progress
        try:
            registration_progress.registration_stage("auth_flow")
            assert progress._lease is not None
            assert progress._lease.group == "auth"
            registration_progress.registration_stage("email_otp_send")
            # Switching stages replaces the lease and releases the previous one.
            assert progress._lease.group == "network"
        finally:
            progress.release_stage_gate()
        assert progress._lease is None


def test_track_registration_releases_gate_even_when_the_call_fails():
    from sms_tool import registration_progress

    with patch.object(registration_concurrency, "CFG", STAGE_CFG), patch.object(
        registration_concurrency, "_stage_gates", {}
    ):
        gate = registration_concurrency._gate_for("auth")
        capacity = _gate_permits(gate)
        captured: list[registration_progress.RegistrationProgress] = []

        @registration_progress.track_registration
        def failing():
            captured.append(registration_progress._current.get())
            registration_progress.registration_stage("auth_flow")
            raise RuntimeError("boom")

        try:
            failing()
        except RuntimeError:
            pass
        else:
            raise AssertionError("track_registration swallowed the failure")

        assert captured and captured[0]._lease is None
        assert _gate_permits(gate) == capacity, "gate leaked when the tracked call failed"
