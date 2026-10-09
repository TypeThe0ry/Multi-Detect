"""CPU lifecycle stand-ins; no native Torch/HF/processor/model acceptance claim."""

from __future__ import annotations

import copy
import gc
import hashlib
import subprocess
import sys
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_actor import source_sample as source_sample
from test_onboard_navigation_inference_backend import PythonTensorStandIn, TorchStandIn

from multidetect.autonomy import onboard_navigation_actor as actor_module
from multidetect.autonomy import onboard_navigation_supervision as navigation
from multidetect.autonomy.serialization import AutonomyContractError
from scripts import onboard_navigation_balanced_encoding as frozen_encoding
from scripts import onboard_navigation_closed_sample_encoding as closed
from scripts import train_onboard_vlm_navigation_pilot as pilot
from scripts import verify_onboard_navigation_actor_inputs as checker


def _budget(**changes):
    def forbidden_old_validator():
        raise AssertionError("The old optimizer/training validator is out of scope")

    return SimpleNamespace(
        **(
            dict(
                max_pixels_per_image=50176,
                max_input_tokens=4096,
                max_new_tokens=192,
                max_optimizer_steps=280,
                validate=forbidden_old_validator,
            )
            | changes
        )
    )


def _metadata(encoded):
    return {key: value for key, value in encoded.items() if key not in {"prefix", "full", "labels"}}


class EncodingTensorStandIn(PythonTensorStandIn):
    def clone(self):
        return type(self)(self.data, self.dtype, self.device.type, self.requires_grad)

    def __getitem__(self, index):
        if isinstance(index, tuple):
            rows, columns = index
            return type(self)(
                [row[columns] for row in self.data[rows]],
                self.dtype,
                self.device.type,
                self.requires_grad,
            )
        return super().__getitem__(index)


class EncodingTorchStandIn(TorchStandIn):
    @staticmethod
    def tensor(data, *, dtype, device):
        assert device == "cpu"
        return EncodingTensorStandIn(data, dtype)


class EncodingProcessorStandIn:
    """Fresh actor messages/PIL/kwargs, with real source RGB but no tokenizer."""

    def __init__(self, *, mutation=None, failure=None, after_full=None):
        self.image_processor = SimpleNamespace(patch_size=16, temporal_patch_size=2)
        specials = dict(
            zip(
                ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>"),
                (17, 18, 19, 47),
                strict=True,
            )
        )
        self.tokenizer = SimpleNamespace(convert_tokens_to_ids=specials.__getitem__)
        self.calls, self.images, self.options = [], [], []
        self.mutation, self.failure, self.after_full = mutation, failure, after_full

    def apply_chat_template(self, messages, **kwargs):
        generation = kwargs["add_generation_prompt"]
        assert kwargs["tokenize"] is True and kwargs["return_dict"] is True
        assert kwargs["return_tensors"] == "pt"
        assert [m["role"] for m in messages] == (
            ["system", "user"] if generation else ["system", "user", "assistant"]
        )
        options = kwargs["images_kwargs"]
        assert options == {"min_pixels": 1024, "max_pixels": 50176}
        self.options.append(options)
        options.clear()
        texts, images = [], []
        for message in messages[:2]:
            for item in message["content"]:
                if item["type"] == "text":
                    texts.append(item["text"])
                else:
                    image = item["image"]
                    assert image.mode == "RGB" and not image.info
                    images.append(image)
                    texts.append(hashlib.sha256(image.tobytes()).hexdigest())
        target = None if generation else messages[-1]["content"][0]["text"]
        self.calls.append((texts, target))
        self.images.extend(images)
        token = int(hashlib.sha256("\n".join(texts).encode()).hexdigest()[:12], 16)
        suffix = [] if generation else [41, 17, 47, 18, 43, 19, 45, 0]
        ids, mask = [token] * 3 + suffix, [1] * (3 + len(suffix))
        if suffix:
            mask[-1] = 0
        pixels = [[0.25] * 1536 for _ in range(4 * len(images))]
        grids = [[1, 2, 2] for _ in images]
        if self.mutation == "signed_zero":
            pixels[0][0] = 0.0 if generation else -0.0
        if not generation:
            if self.mutation == "ids":
                ids[0] += 1
            elif self.mutation == "mask":
                mask[0] = 0
            elif self.mutation == "grid":
                grids[0] = [1, 1, 4]
            elif self.mutation == "pixels":
                pixels[0][0] += 1
            if self.failure is not None:
                raise self.failure("Injected full-chat processing failure")
        result = {
            "input_ids": EncodingTensorStandIn([ids]),
            "attention_mask": EncodingTensorStandIn([mask]),
            "image_grid_thw": EncodingTensorStandIn(grids),
            "pixel_values": EncodingTensorStandIn(pixels, "torch.float32"),
        }
        for message in messages:
            message["content"].clear()
        if not generation and self.after_full is not None:
            self.after_full()
        return result


def _reference(sample, root, processor=None):
    # Local stand-in reference, never the sealed official processor CPU report.
    return checker.verify_sample_pair(
        sample,
        root,
        None,
        processor or EncodingProcessorStandIn(),
        actor_module=actor_module,
        pilot=pilot,
        budget=navigation.NavigationSupervisionBudget(),
        torch_module=EncodingTorchStandIn(),
    )


def _assert_images_closed(images):
    for image in images:
        with pytest.raises(ValueError):
            image.tobytes()


class ObservedSourceSession(closed.ClosedNavigationSourceValidationSession):
    def __init__(self, root, processor):
        super().__init__(root)
        self.processor = processor
        self.events, self.primes, self.validated, self.source_images = [], [], [], []

    def __enter__(self):
        self.events.append("enter")
        return super().__enter__()

    def prime(self, samples):
        self.events.append("prime")
        self.primes.append(samples)
        return super().prime(samples)

    def validate_sample(self, sample):
        self.events.append("validate_sample")
        self.validated.append(sample)
        images = super().validate_sample(sample)
        self.source_images.extend(images)
        return images

    def __exit__(self, error_type, error, traceback):
        self.events.append(("exit_start", error_type))
        _assert_images_closed(self.source_images + self.processor.images)
        try:
            return super().__exit__(error_type, error, traceback)
        finally:
            self.events.append(("exit_end", self.verified))


def _native_fixture_call(sample, root, processor, reference, **kwargs):
    return closed.encode_closed_navigation_sample(
        sample,
        root,
        processor=processor,
        torch_module=EncodingTorchStandIn(),
        budget=_budget(),
        prefix_reference=reference,
        **kwargs,
    )


@pytest.mark.parametrize("image_count", [1, 2])
def test_real_source_session_closes_before_return_and_preserves_old_bytes(
    source_sample, tmp_path, image_count
):
    sample = replace(source_sample, frames=source_sample.frames[-image_count:])
    reference = _reference(sample, tmp_path)
    old_processor = EncodingProcessorStandIn()
    with navigation.NavigationSourceValidationSession(tmp_path) as old_session:
        old_session.prime((sample,))
        old = frozen_encoding.encode_navigation_sample(
            sample,
            tmp_path,
            processor=old_processor,
            torch_module=EncodingTorchStandIn(),
            budget=_budget(),
            prefix_reference=reference,
            session=old_session,
        )
        assert not old_session.verified
    assert old_session.verified
    processor = EncodingProcessorStandIn()
    sessions, returned = [], []

    def factory(root):
        session = ObservedSourceSession(root, processor)
        sessions.append(session)
        return session

    def observe_encoder(*args, **kwargs):
        assert args[0] is sample and kwargs["session"] is sessions[0]
        value = frozen_encoding.encode_navigation_sample(*args, **kwargs)
        assert kwargs["session"].verified is False
        returned.append(value)
        return value

    result = _native_fixture_call(
        sample,
        tmp_path,
        processor,
        reference,
        session_factory=factory,
        encoder=observe_encoder,
        expected_metadata=_metadata(old),
    )
    session = sessions[0]
    assert len(sessions) == 1 and session.verified is True and not session._active
    assert session.events[-2:] == [("exit_start", None), ("exit_end", True)]
    assert all(type(samples) is tuple and samples == (sample,) for samples in session.primes)
    assert all(samples[0] is sample for samples in session.primes)
    assert session.validated == [sample, sample]  # Actor and target share this session/sample.
    assert all(value is sample for value in session.validated)
    assert result is returned[0]
    assert _metadata(result) == _metadata(old)
    for category in ("prefix", "full"):
        assert set(result[category]) == checker.FIELDS
        for name in checker.FIELDS:
            assert pilot.tensor_record(result[category][name], EncodingTorchStandIn()) == (
                pilot.tensor_record(old[category][name], EncodingTorchStandIn())
            )
    assert (
        result["labels"].tolist()
        == old["labels"].tolist()
        == [[-100] * 3 + [41, -100, -100, -100, 43, -100, 45, -100]]
    )
    assert result["prefix_tensors"] == reference["training_prefix"]["tensors"]
    assert processor.calls[0][0] == processor.calls[1][0]
    assert processor.calls[0][1] is None
    target = navigation.serialize_navigation_supervision_target(sample, tmp_path)
    assert processor.calls[1][1] == target
    assert result["target_sha256"] == hashlib.sha256(target.encode()).hexdigest()
    assert result["actor_identity_sha256"] == reference["actor_identity_sha256"]
    assert processor.options[0] is not processor.options[1]
    assert len({id(image) for image in processor.images}) == 2 * image_count
    _assert_images_closed(processor.images + session.source_images)


@pytest.mark.parametrize("failure", [RuntimeError, MemoryError])
def test_frozen_processor_exception_closes_images_and_never_final_verifies(
    source_sample, tmp_path, failure
):
    reference = _reference(source_sample, tmp_path)
    processor = EncodingProcessorStandIn(failure=failure)
    sessions, consumed = [], []

    def factory(root):
        session = ObservedSourceSession(root, processor)
        sessions.append(session)
        return session

    with pytest.raises(failure, match="processing failure"):
        consumed.append(
            _native_fixture_call(
                source_sample, tmp_path, processor, reference, session_factory=factory
            )
        )
    assert not consumed and not sessions[0].verified and not sessions[0]._active
    assert sessions[0].events[-2:] == [("exit_start", failure), ("exit_end", False)]
    _assert_images_closed(processor.images + sessions[0].source_images)


def test_final_source_sha_drift_prevents_downstream_admission(source_sample, tmp_path):
    reference = _reference(source_sample, tmp_path)
    path = tmp_path / source_sample.source.annotation.path

    def drift():
        path.write_bytes(path.read_bytes() + b" ")

    processor = EncodingProcessorStandIn(after_full=drift)
    sessions, losses = [], []

    def factory(root):
        session = ObservedSourceSession(root, processor)
        sessions.append(session)
        return session

    with pytest.raises(AutonomyContractError):
        result = _native_fixture_call(
            source_sample, tmp_path, processor, reference, session_factory=factory
        )
        losses.append(result)
    assert not losses and not sessions[0].verified and not sessions[0]._active
    assert sessions[0].events[-2:] == [("exit_start", None), ("exit_end", False)]


@pytest.mark.parametrize("mutation", ["ids", "mask", "grid", "pixels", "signed_zero"])
def test_frozen_exact_four_tensor_prefix_gate_is_not_bypassed(source_sample, tmp_path, mutation):
    reference = _reference(
        source_sample,
        tmp_path,
        EncodingProcessorStandIn(mutation="signed_zero") if mutation == "signed_zero" else None,
    )
    processor = EncodingProcessorStandIn(mutation=mutation)
    with pytest.raises(ValueError, match="exact token/mask/grid/pixel prefix bytes"):
        _native_fixture_call(source_sample, tmp_path, processor, reference)
    _assert_images_closed(processor.images)


def test_complete_frozen_prefix_reference_still_required(source_sample, tmp_path):
    reference = _reference(source_sample, tmp_path)
    reference["retained_history_valid"] = 1
    with pytest.raises(ValueError, match="Complete CPU86 prefix reference"):
        _native_fixture_call(source_sample, tmp_path, EncodingProcessorStandIn(), reference)


def test_real_source_factory_cannot_bypass_closed_validation_image_ownership(
    source_sample, tmp_path
):
    processor = EncodingProcessorStandIn()
    session = navigation.NavigationSourceValidationSession(tmp_path)
    with pytest.raises(ValueError, match="closed validation-image ownership"):
        _native_fixture_call(
            source_sample,
            tmp_path,
            processor,
            {},
            session_factory=lambda root: session,
        )
    assert not session._used and not processor.calls


def test_real_closed_session_cannot_be_reused_between_accesses(source_sample, tmp_path):
    reference = _reference(source_sample, tmp_path)
    processor = EncodingProcessorStandIn()
    session = closed.ClosedNavigationSourceValidationSession(tmp_path)
    result = _native_fixture_call(
        source_sample,
        tmp_path,
        processor,
        reference,
        session_factory=lambda root: session,
    )
    assert session.verified is True
    assert result["prefix_reference_verified"] is True
    assert len(processor.calls) == 2
    with pytest.raises(AutonomyContractError, match="cannot be reused"):
        _native_fixture_call(
            source_sample,
            tmp_path,
            processor,
            reference,
            session_factory=lambda root: session,
        )
    assert len(processor.calls) == 2


class TensorLease:
    pass


def _provisional():
    record = {"shape": [1, 3], "dtype": "torch.int64", "sha256": "a" * 64}
    return {
        "prefix": TensorLease(),
        "full": TensorLease(),
        "labels": TensorLease(),
        "prefix_tokens": 3,
        "total_tokens": 11,
        "response_tokens": 8,
        "supervised_tokens": 3,
        "prefix_tensors": {key: copy.deepcopy(record) for key in sorted(checker.FIELDS)},
        "input_tensors": {key: copy.deepcopy(record) for key in sorted(checker.FIELDS)},
        "response_labels_sha256": "b" * 64,
        "target_sha256": "c" * 64,
        "actor_identity_sha256": "d" * 64,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
    }


class SessionStandIn:
    def __init__(self, root, *, verified=True, fail_at=None, failure=RuntimeError, suppress=False):
        self.root, self.exit_value, self.fail_at = root, verified, fail_at
        self.failure, self.suppress = failure, suppress
        self.verified = False
        self.events = []

    def __enter__(self):
        self.events.append("enter")
        if self.fail_at == "enter":
            raise self.failure("enter failed")
        return object()  # The factory session, not this unrelated value, must reach encoding.

    def prime(self, samples):
        self.events.append(("prime", samples))
        if self.fail_at == "prime":
            raise self.failure("prime failed")

    def __exit__(self, error_type, error, traceback):
        self.events.append(("exit", error_type))
        if self.fail_at == "exit":
            raise self.failure("exit failed")
        if error_type is None or self.suppress:
            self.verified = self.exit_value
        return self.suppress


def _injected_call(tmp_path, **options):
    return closed.encode_closed_navigation_sample(
        options.pop("sample", object()),
        tmp_path,
        processor=object(),
        torch_module=object(),
        budget=options.pop("budget", _budget()),
        prefix_reference={"local_stand_in": True},
        **options,
    )


@pytest.mark.parametrize("point", ["enter", "prime", "encoder", "exit"])
@pytest.mark.parametrize("failure", [RuntimeError, MemoryError])
def test_any_lifecycle_exception_prevents_result_access_loss_or_step(tmp_path, point, failure):
    sessions, refs, downstream = [], [], {"accesses": 0, "losses": 0, "steps": 0}

    def factory(root):
        session = SessionStandIn(root, fail_at=point, failure=failure)
        sessions.append(session)
        return session

    def encoder(*args, **kwargs):
        assert kwargs["session"] is sessions[0] and kwargs["session"].verified is False
        if point == "encoder":
            raise failure("encoder failed")
        result = _provisional()
        refs.extend(weakref.ref(result[key]) for key in ("prefix", "full", "labels"))
        return result

    with pytest.raises(failure, match="failed"):
        _injected_call(tmp_path, session_factory=factory, encoder=encoder)
        downstream["accesses"] += 1
        downstream["losses"] += 1
        downstream["steps"] += 1
    assert downstream == {"accesses": 0, "losses": 0, "steps": 0}
    gc.collect()
    assert all(reference() is None for reference in refs)
    if point in {"prime", "encoder"}:
        assert sessions[0].events[-1] == ("exit", failure)


@pytest.mark.parametrize("verified", [False, None, 1, "true"])
def test_normal_exit_must_verify_with_exact_boolean_true(tmp_path, verified):
    session = SessionStandIn(tmp_path, verified=verified)
    with pytest.raises(ValueError, match="normal exit and verified=True"):
        _injected_call(
            tmp_path, session_factory=lambda root: session, encoder=lambda *a, **k: _provisional()
        )


@pytest.mark.parametrize("point", ["prime", "encoder"])
def test_suppressed_body_error_cannot_be_admitted_even_if_session_claims_verified(tmp_path, point):
    session = SessionStandIn(tmp_path, fail_at=point, suppress=True)

    def encoder(*args, **kwargs):
        raise MemoryError("encoder failed")

    with pytest.raises(ValueError, match="normal exit"):
        _injected_call(tmp_path, session_factory=lambda root: session, encoder=encoder)
    assert session.verified is True


@pytest.mark.parametrize("field", list(_metadata(_provisional())) + ["extra", "missing", "nan"])
def test_full_metadata_parity_checks_all_keys_and_is_after_verified_exit(tmp_path, field):
    expected = _metadata(_provisional())
    value = _provisional()
    if field == "extra":
        value["unknown"] = False
    elif field == "missing":
        del value["target_sha256"]
    elif field == "nan":
        value["total_tokens"] = float("nan")
    elif field in {"prefix_tensors", "input_tensors"}:
        value[field]["pixel_values"]["sha256"] = "e" * 64
    elif field in {"prefix_reference_verified", "prompt_label_mask_verified"}:
        value[field] = 1
    else:
        value[field] = "changed"
    session = SessionStandIn(tmp_path)
    with pytest.raises(ValueError):
        _injected_call(
            tmp_path,
            expected_metadata=expected,
            session_factory=lambda root: session,
            encoder=lambda *args, **kwargs: value,
        )
    assert session.verified is True and session.events[-1] == ("exit", None)


def test_expected_metadata_is_snapshotted_not_mutable_shared_alias(tmp_path):
    value = _provisional()
    expected = _metadata(value)
    session = SessionStandIn(tmp_path)

    def encoder(*args, **kwargs):
        assert session.verified is False
        value["prefix_tensors"]["input_ids"]["sha256"] = "e" * 64
        assert expected["prefix_tensors"]["input_ids"]["sha256"] == "e" * 64
        return value

    with pytest.raises(ValueError, match="Complete encoding metadata"):
        _injected_call(
            tmp_path,
            session_factory=lambda root: session,
            encoder=encoder,
            expected_metadata=expected,
        )


def test_fresh_per_access_session_arguments_and_no_tensor_cache(tmp_path):
    sample, processor, torch_module, budget, reference = object(), object(), object(), _budget(), {}
    sessions, refs = [], []

    def factory(root):
        assert root == tmp_path and isinstance(root, Path)
        session = SessionStandIn(root)
        sessions.append(session)
        return session

    def encoder(actual_sample, actual_root, **kwargs):
        assert actual_sample is sample and actual_root is tmp_path
        assert kwargs == dict(
            processor=processor,
            torch_module=torch_module,
            budget=budget,
            prefix_reference=reference,
            session=sessions[-1],
        )
        assert sessions[-1].events == ["enter", ("prime", (sample,))]
        assert not sessions[-1].verified
        result = _provisional()
        refs.append(weakref.ref(result["full"]))
        return result

    def call():
        return closed.encode_closed_navigation_sample(
            sample,
            tmp_path,
            processor=processor,
            torch_module=torch_module,
            budget=budget,
            prefix_reference=reference,
            expected_metadata=_metadata(_provisional()),
            session_factory=factory,
            encoder=encoder,
        )

    first = call()
    assert sessions[0].verified is True and refs[0]() is first["full"]
    del first
    gc.collect()
    assert refs[0]() is None
    second = call()
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert sessions[1].verified is True and refs[1]() is second["full"]
    del second
    gc.collect()
    assert all(reference() is None for reference in refs)


@pytest.mark.parametrize("value", [None, [], 1])
def test_non_mapping_encoder_result_rejected_before_normal_verification(tmp_path, value):
    session = SessionStandIn(tmp_path)
    with pytest.raises(ValueError, match="existing encoding mapping"):
        _injected_call(
            tmp_path, session_factory=lambda root: session, encoder=lambda *a, **k: value
        )
    assert not session.verified and session.events[-1] == ("exit", ValueError)


@pytest.mark.parametrize(
    "changes", [{"max_pixels_per_image": True}, {"max_input_tokens": 4097}, {"max_new_tokens": 193}]
)
def test_encoding_budget_rejected_before_session_creation(tmp_path, changes):
    def forbidden_factory(root):
        raise AssertionError("Bad budget must fail before creating a session")

    with pytest.raises(ValueError, match="50176/4096/192"):
        _injected_call(tmp_path, budget=_budget(**changes), session_factory=forbidden_factory)


def test_guarded_static_import_has_no_heavy_dependencies():
    path = Path(closed.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"sys.path.insert(0,{str(path.parents[1] / 'src')!r}); "
        f"spec=importlib.util.spec_from_file_location('closed_encoding',{str(path)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','PIL','peft','datasets','accelerate'} "
        "& set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
