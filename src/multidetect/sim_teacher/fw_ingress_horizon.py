"""Capture-side evidence for the single FW confirmation on an X1 survey.

Only the collector thread can write a video row. Its last transition-to-FW
-1 tick has returned before confirmation; the survey has no subsequent -1
writer. A GET response only updates the sensor reader, not the manifest. The
controller, frame ownership, capture schedule and scoring are not changed here.
"""


def _stamp(value):
    return type(value) is int and value >= 0


class FwIngressCaptureGuard:
    def __init__(self):
        self.last_commit_ns = None
        self.pending_due_ns = None
        self._invalid = False

    def note_request(self, due_ns):
        """Only an actual -1 video request; retries for this due are the same work."""
        if not _stamp(due_ns) or self.pending_due_ns not in (None, due_ns):
            self._invalid = True
        else:
            self.pending_due_ns = due_ns

    def note_commit(self, stamp_ns):
        """Only after writer.write(..., action_index=-1) returns True."""
        if (
            not _stamp(stamp_ns)
            or (self.last_commit_ns is not None and stamp_ns <= self.last_commit_ns)
            or (self.pending_due_ns is not None and stamp_ns < self.pending_due_ns)
        ):
            self._invalid = True
            return
        self.last_commit_ns = stamp_ns
        self.pending_due_ns = None

    def evidence(self, *, next_video_sim_ns, vehicle_time_ms):
        """One non-blocking snapshot at FW confirmation, not a new scoring marker."""
        if self._invalid:
            reason = "pre_route_video_history_invalid"
        elif self.last_commit_ns is None:
            reason = "pre_route_video_not_committed"
        elif self.pending_due_ns is not None:
            reason = "pre_route_video_request_pending"
        elif (
            not _stamp(next_video_sim_ns)
            or not _stamp(vehicle_time_ms)
            or next_video_sim_ns <= self.last_commit_ns
            or vehicle_time_ms * 1_000_000 < self.last_commit_ns
        ):
            reason = "pre_route_video_clock_unproven"
        elif vehicle_time_ms * 1_000_000 >= next_video_sim_ns:
            reason = "pre_route_video_due"
        else:
            reason = None
        return {
            "skip_reason": reason,
            "last_pre_route_video_sim_ns": self.last_commit_ns,
            "pending_pre_route_video_due_ns": self.pending_due_ns,
            "next_video_sim_ns": next_video_sim_ns,
            "vehicle_time_boot_ms": vehicle_time_ms,
        }
