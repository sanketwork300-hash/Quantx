"""Job lifecycle transitions."""

from __future__ import annotations

import pytest

from domains.jobs.models import ALLOWED_TRANSITIONS, JobStatus


class TestJobStatus:
    @pytest.mark.parametrize(
        "status,terminal",
        [
            (JobStatus.QUEUED, False),
            (JobStatus.RUNNING, False),
            (JobStatus.COMPLETED, True),
            (JobStatus.FAILED, True),
            (JobStatus.CANCELLED, True),
        ],
    )
    def test_terminality(self, status, terminal):
        assert status.is_terminal is terminal

    def test_terminal_states_admit_no_transitions(self):
        """A worker finishing late must not resurrect a cancelled job."""
        for status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_queued_may_start_cancel_or_fail(self):
        assert ALLOWED_TRANSITIONS[JobStatus.QUEUED] == frozenset(
            {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}
        )

    def test_running_may_only_reach_a_terminal_state(self):
        assert all(target.is_terminal for target in ALLOWED_TRANSITIONS[JobStatus.RUNNING])

    def test_a_job_cannot_return_to_queued(self):
        assert all(JobStatus.QUEUED not in targets for targets in ALLOWED_TRANSITIONS.values())

    def test_every_status_has_a_transition_rule(self):
        assert set(ALLOWED_TRANSITIONS) == set(JobStatus)
