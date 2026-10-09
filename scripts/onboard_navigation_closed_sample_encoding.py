"""One-sample source closure before an existing CPU encoding is admitted.

This is not a loader, dataset, or trainer. The caller admits the processor and
the SHA-bound prefix report, and may consume/count an encoding only on return.
Without ``expected_metadata`` this low-level helper does not claim full-row
CPU metadata parity. Production recovery callers must supply the complete
metadata row from their independently admitted prepared CPU report.
"""

from __future__ import annotations

import json
from pathlib import Path

from multidetect.autonomy.onboard_navigation_supervision import NavigationSourceValidationSession


class ClosedNavigationSourceValidationSession(NavigationSourceValidationSession):
    """Validation-only decoded images are closed before encoding continues.

    The frozen actor closes these returned images without reading them; the
    frozen target serializer ignores them. Neither needs a live validation
    image. Fresh actor/message decodes remain owned by the frozen encoder.
    Timing factories may subclass this session, preserving normal final SHA
    semantics without holding PIL references through the whole encoding.

    This does not change the frozen base's partial-decode exception cleanup:
    if it raises before returning its tuple, no result or final SHA is admitted.
    """

    def validate_sample(self, sample):
        images = super().validate_sample(sample)
        try:
            return images
        finally:
            for image in images:
                image.close()


def _metadata_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def encode_closed_navigation_sample(
    sample,
    artifact_root,
    *,
    processor,
    torch_module,
    budget,
    prefix_reference,
    expected_metadata=None,
    session_factory=None,
    encoder=None,
):
    """Return the unchanged encoding only after this sample's verified exit.

    ``session_factory(Path(artifact_root))`` creates a fresh source-validation
    session, including a timed subclass if desired. It is primed with exactly
    ``(sample,)`` and passed unchanged to the frozen encoder. The encoder owns
    message-image cleanup. The default session closes validation-only images
    immediately; injected real source sessions must preserve that contract.
    Normal final source verification runs only after the encoder has finished
    and closed its message images. A failed body, exit,
    final SHA, or metadata comparison cannot expose a provisional result.

    Injection is for bounded CPU orchestration tests/profiling; it does not
    certify an injected processor, tensor implementation, or source session.
    No tensors are cached and no access/loss/optimizer counters are changed.
    """
    from scripts import onboard_navigation_balanced_encoding as encoding

    encoding.validate_encoding_budget(budget)
    if expected_metadata is not None and type(expected_metadata) is not dict:
        raise ValueError("Expected metadata must be a complete encoding metadata mapping")
    expected = None if expected_metadata is None else _metadata_bytes(expected_metadata)
    if session_factory is None:
        session_factory = ClosedNavigationSourceValidationSession
    if encoder is None:
        encoder = encoding.encode_navigation_sample
    if not callable(session_factory) or not callable(encoder):
        raise TypeError("Session factory and encoder must be callable")

    session = session_factory(Path(artifact_root))
    if isinstance(session, NavigationSourceValidationSession) and not isinstance(
        session, ClosedNavigationSourceValidationSession
    ):
        raise ValueError("Real source sessions must preserve closed validation-image ownership")
    encoded = None
    body_completed = False
    try:
        with session:
            session.prime((sample,))
            encoded = encoder(
                sample,
                artifact_root,
                processor=processor,
                torch_module=torch_module,
                budget=budget,
                prefix_reference=prefix_reference,
                session=session,
            )
            if type(encoded) is not dict:
                raise ValueError("Encoder must return the existing encoding mapping")
            body_completed = True
        # A suppressing injected context must not turn a failed body into admission.
        if not body_completed or getattr(session, "verified", False) is not True:
            raise ValueError("Sample encoding requires normal exit and verified=True")
        if expected is not None:
            actual = {
                key: value
                for key, value in encoded.items()
                if key not in {"prefix", "full", "labels"}
            }
            if _metadata_bytes(actual) != expected:
                raise ValueError("Complete encoding metadata differs from the admitted CPU row")
        return encoded
    except BaseException:
        # Do not retain provisional tensors in this helper's failure traceback.
        encoded = None
        raise
