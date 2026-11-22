"""Exit status shared by the X1 worker and its launch-pad checks."""

PAD_REJECTED = 42


class PadRejected(ValueError):
    """A different launch origin could resolve this specific failure."""
