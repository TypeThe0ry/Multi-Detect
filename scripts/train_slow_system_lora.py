"""Offline Qwen3-VL slow-decision SFT. Run only after the GPU is free.

Only train.jsonl enters the optimizer. Model and processor load from local files;
there is no network fallback. Checkpoints permit deterministic epoch/order resume.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Must be set before torch initialises CUDA: limits allocator fragmentation on the shared
# 8 GB GPU, where long runs otherwise fail with "CUDA error: out of memory".
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")
# A small fixed cuBLAS workspace: cuBLAS allocates outside the caching allocator and a
# failed workspace allocation poisons the CUDA context ("CUDA error: out of memory").
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

from multidetect.autonomy.onboard_decision import parse_onboard_decision
from scripts.slow_system_runtime import ROOT, chat_messages, dataset_crop_policy, load_jsonl
from scripts.train_onboard_vlm_grounding import causal_response_loss, response_only_labels

DEFAULT_CACHE = (
    ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots"
)
DEFAULT_DATASET = ROOT / "artifacts/datasets/slow-system-sft-r1"
LANGUAGE_PROJECTION = re.compile(r"model\.language_model\.layers\.\d+\.self_attn\.(q_proj|v_proj)")
VISION_PROJECTOR = re.compile(r"model\.visual\.merger\.(?:linear_fc1|linear_fc2)")


@dataclass(frozen=True)
class TrainOptions:
    epochs: int = 1
    max_steps: int = 0
    accumulation: int = 4
    rank: int = 4
    alpha: int = 8
    learning_rate: float = 1e-4
    seed: int = 20261011
    max_input_tokens: int = 2048
    max_new_tokens: int = 128
    checkpoint_every: int = 50
    max_side: int = 448
    quantization: str = "auto"
    vision_projector: bool = False
    crop_policy: str = "none"
    balance: bool = False

    def validate(self) -> None:
        for name in (
            "epochs",
            "accumulation",
            "rank",
            "alpha",
            "max_input_tokens",
            "max_new_tokens",
            "checkpoint_every",
            "max_side",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_steps < 0 or self.quantization not in {"auto", "4bit", "bf16"}:
            raise ValueError("invalid max_steps or quantization")
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError("learning_rate must be finite and positive")


def local_snapshot(path: Path) -> Path:
    if path.is_dir() and (path / "config.json").is_file():
        return path.resolve()
    snapshots = sorted(p for p in path.glob("*/config.json") if p.is_file())
    if len(snapshots) != 1:
        raise ValueError(
            f"Expected one complete local Qwen snapshot under {path}; found {len(snapshots)}"
        )
    return snapshots[0].parent.resolve()


def resolve_quantization(choice: str, *, bnb_available: bool | None = None) -> str:
    if bnb_available is None:
        bnb_available = importlib.util.find_spec("bitsandbytes") is not None
    if choice == "4bit" and not bnb_available:
        raise RuntimeError("4-bit was requested but bitsandbytes is unavailable locally")
    return (
        "4bit" if choice == "auto" and bnb_available else ("bf16" if choice == "auto" else choice)
    )


def model_load_kwargs(options: TrainOptions, *, bnb_available: bool | None = None) -> dict:
    """Pure option plumbing so CPU tests can assert the 8 GB load strategy."""
    options.validate()
    quantization = resolve_quantization(options.quantization, bnb_available=bnb_available)
    kwargs = {
        "local_files_only": True,
        "trust_remote_code": False,
        "torch_dtype": "bfloat16",
        "attn_implementation": "sdpa",
        "device_map": {"": "cuda:0"},
    }
    if quantization == "4bit":
        kwargs["load_in_4bit"] = True
        kwargs["bnb_4bit_quant_type"] = "nf4"
        kwargs["bnb_4bit_compute_dtype"] = "bfloat16"
    return kwargs


def _input_hash(rows: list[dict]) -> str:
    data = "\n".join(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows).encode(
        "utf-8"
    )
    return hashlib.sha256(data).hexdigest()


def admit_train_rows(dataset: Path) -> tuple[list[dict], str]:
    rows = load_jsonl(dataset / "train.jsonl", split="train")
    if not rows:
        raise ValueError("train split is empty")
    identifiers = set()
    for row in rows:
        if row["sample_id"] in identifiers:
            raise ValueError("duplicate sample_id in train")
        identifiers.add(row["sample_id"])
        if row.get("kind") == "mission_plan":
            from multidetect.autonomy.capability_contract import (
                capability_violations,
                normalize_capabilities,
            )
            from multidetect.autonomy.mission_plan import validate_mission_plan
            from multidetect.autonomy.plan_intent import check_plan_intent
            from multidetect.autonomy.mission_planner import check_plan_refs

            capabilities = normalize_capabilities(row.get("capabilities", ()))
            if row["assistant_target"].get("decision") in {"reject", "clarify"}:
                from multidetect.autonomy.mission_plan import validate_plan_decision
                decision = validate_plan_decision(row["assistant_target"])
                if row.get("expected_valid", True) is not False:
                    raise ValueError(f"refusal target {row['sample_id']} must be marked expected_valid=false")
                expected = set(row.get("expected_findings", ()))
                if expected and expected != {decision.reason_code}:
                    raise ValueError(f"refusal target {row['sample_id']} reason_code mismatch")
                if not row.get("world") or not row.get("task_text"):
                    raise ValueError("missing model input or world")
                candidate = row.get("rejected_candidate")
                if candidate is not None:
                    if not isinstance(candidate, dict):
                        raise ValueError(f"refusal audit candidate {row['sample_id']} must be an object")
                    audit_capability_codes = {
                        item["code"] for item in capability_violations(
                            candidate, capabilities,
                            airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
                        )
                    }
                    audit_intent_codes = {
                        item.code for item in check_plan_intent(
                            candidate, row["task_text"], tuple(row.get("refs", ()))
                        )
                    }
                    _, _, audit_unknown_refs = check_plan_refs(
                        candidate, tuple(row.get("refs", ())), substitute=False
                    )
                    if audit_unknown_refs:
                        audit_intent_codes.add("ungrounded_place")
                    validate_mission_plan(
                        candidate,
                        capabilities=frozenset({"payload_drop", "hover", "landing_on_target"}),
                    )
                    expected_audit_code = {
                        "unknown_place": "ungrounded_place",
                    }.get(decision.reason_code, decision.reason_code)
                    if expected_audit_code not in audit_capability_codes | audit_intent_codes:
                        raise ValueError(
                            f"refusal audit candidate {row['sample_id']} does not trigger "
                            f"{expected_audit_code}"
                        )
                continue
            task_text = str(row.get("task_text", ""))
            task_fold = task_text.casefold()
            known_refs = {str(ref).split(" ", 1)[0] for ref in row.get("refs", ())}
            # Every semantic value in a target must be grounded in the request,
            # the supplied refs, or the declared capability contract.
            for step in row["assistant_target"].get("steps", ()):
                params = step.get("params", {})
                target_ref = params.get("target_ref") or params.get("region")
                if isinstance(target_ref, str) and target_ref.startswith("poi:"):
                    ref_name = target_ref[4:].replace("_", " ").casefold()
                    if (row.get("refs") and target_ref not in known_refs
                            and ref_name not in task_fold
                            and not (row.get("expected_valid") is False
                                     and "ungrounded_place" in row.get("expected_findings", ()) )):
                        raise ValueError(f"target {row['sample_id']} invents ungrounded place: {target_ref}")
                query = params.get("query")
                if query is not None and str(query).strip() and row.get("family") in {
                    "query_grounding", "missing_payload_drop", "unknown_place", "ref_alias",
                    "fixed_wing_constraint", "fixed_wing_target_land", "return_intent",
                }:
                    q = str(query).casefold()
                    query_aliases = {
                        "bicycle": ("bicycle", "bicycles", "自行车"),
                        "red car": ("red car", "红色轿车"),
                        "fire truck": ("fire truck", "消防车"),
                        "ambulance": ("ambulance", "救护车"),
                    }
                    grounded_terms = query_aliases.get(q, (q, q.rstrip("s")))
                    if not any(term in task_fold for term in grounded_terms):
                        raise ValueError(f"target {row['sample_id']} invents ungrounded query: {query}")
                payload_ref = params.get("payload_ref")
                if payload_ref is not None and "parcel" in str(payload_ref).casefold() and not any(
                    token in task_fold for token in ("parcel", "package", "包裹")
                ):
                    raise ValueError(f"target {row['sample_id']} invents ungrounded payload: {payload_ref}")
            findings = capability_violations(
                row["assistant_target"],
                capabilities,
                airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
            )
            intent_codes = {
                finding.code
                for finding in check_plan_intent(
                    row["assistant_target"], row.get("task_text", ""), tuple(row.get("refs", ()))
                )
            }
            expected_valid = row.get("expected_valid", True)
            expected_findings = set(row.get("expected_findings", ()))
            if expected_valid is False and "ungrounded_place" in expected_findings:
                known_refs = {
                    str(ref).split(" ", 1)[0] for ref in row.get("refs", ())
                }
                target_refs = {
                    str(step.get("params", {}).get("target_ref"))
                    for step in row["assistant_target"].get("steps", ())
                    if step.get("params", {}).get("target_ref") is not None
                }
                substitutions = sorted(known_refs & target_refs)
                if substitutions:
                    raise ValueError(
                        f"unknown-place target {row['sample_id']} substitutes known ref(s): {substitutions}"
                    )
            if expected_valid is False:
                candidate = row.get("rejected_candidate")
                if not isinstance(candidate, dict) or not expected_findings:
                    raise ValueError(f"rejected mission row {row['sample_id']} lacks its candidate audit record")
                candidate_codes = {
                    finding["code"]
                    for finding in capability_violations(
                        candidate, capabilities,
                        airframe="airframe:fixed_wing" if "airframe:fixed_wing" in capabilities else None,
                    )
                } | {
                    finding.code
                    for finding in check_plan_intent(candidate, row["task_text"], tuple(row.get("refs", ())))
                }
                _, _, unknown_refs = check_plan_refs(
                    candidate, tuple(row.get("refs", ())), substitute=False
                )
                if unknown_refs:
                    candidate_codes.add("ungrounded_place")
                if not expected_findings <= candidate_codes:
                    raise ValueError(f"rejected candidate {row['sample_id']} does not match expected findings")
                # r22 trains the validator-triggering candidate itself.  The
                # target is intentionally invalid for the row contract, so
                # validate only its structural mission shape with all gated
                # capabilities enabled; the expected finding is the label.
                target_is_candidate = row.get("negative_target_mode") == "rejected_candidate"
                if target_is_candidate:
                    validate_mission_plan(
                        row["assistant_target"],
                        capabilities=frozenset({"payload_drop", "hover", "landing_on_target"}),
                    )
            elif findings or intent_codes:
                raise ValueError(
                    f"valid mission row {row['sample_id']} violates planner contract: "
                    f"{sorted({finding['code'] for finding in findings} | intent_codes)}"
                )
            if row.get("expected_valid", True) is not False or row.get("negative_target_mode") != "rejected_candidate":
                validate_mission_plan(row["assistant_target"], capabilities=capabilities)
        else:
            parse_onboard_decision(row["assistant_target"])
        if not row.get("world") or not row.get("task_text"):
            raise ValueError("missing model input or world")
    return rows, _input_hash(rows)


def encode_response(
    processor, row: dict, *, max_side: int, max_input_tokens: int, crop_policy: str | None = None
):
    import torch

    messages = chat_messages(row, max_side, crop_policy)
    common = {"tokenize": True, "return_dict": True, "return_tensors": "pt"}
    prefix = processor.apply_chat_template(messages, add_generation_prompt=True, **common)
    answer = json.dumps(
        row["assistant_target"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    full = processor.apply_chat_template(
        messages + [{"role": "assistant", "content": [{"type": "text", "text": answer}]}],
        add_generation_prompt=False,
        **common,
    )
    ids = full["input_ids"][0].tolist()
    prefix_ids = prefix["input_ids"][0].tolist()
    if len(ids) > max_input_tokens:
        raise ValueError(f"sample {row['sample_id']} exceeds max_input_tokens")
    image_tokens = {
        processor.tokenizer.convert_tokens_to_ids(value)
        for value in ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>")
    }
    labels = response_only_labels(ids, prefix_ids, full["attention_mask"][0].tolist(), image_tokens)
    return full, torch.tensor([labels], dtype=torch.long), len(ids) - len(prefix_ids)


def balance_rows(rows: list[dict], *, seed: int) -> list[dict]:
    """Deterministically oversample decision classes to no more than 2:1."""
    names = ("confirm_target", "approach_track", "reject_track")
    groups = {
        name: [
            row for row in rows if row["assistant_target"].get("decision", "mission_plan") == name
        ]
        for name in names
    }
    present = [group for group in groups.values() if group]
    if not present:
        return list(rows)
    target = max(len(group) for group in present)
    rng = random.Random(seed)
    result = [
        row for row in rows if row["assistant_target"].get("decision", "mission_plan") not in names
    ]
    for name in names:
        group = groups[name]
        if group:
            result.extend(group)
            result.extend(group[rng.randrange(len(group))] for _ in range(target - len(group)))
    return result


def make_backend(snapshot: Path, options: TrainOptions):
    import peft
    import torch
    import transformers

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("Qwen3-VL LoRA training requires a free BF16 CUDA GPU")
    # Cap the caching allocator below the card size so kernels/cuBLAS keep headroom on a GPU
    # shared with the desktop; an over-budget batch then raises a catchable allocator OOM.
    torch.cuda.set_per_process_memory_fraction(
        float(os.environ.get("SLOW_TRAIN_GPU_MEMORY_FRACTION", "0.88"))
    )
    quantization = resolve_quantization(options.quantization)
    torch.manual_seed(options.seed)
    torch.cuda.manual_seed_all(options.seed)
    processor = transformers.AutoProcessor.from_pretrained(
        str(snapshot),
        local_files_only=True,
        trust_remote_code=False,
        max_pixels=options.max_side * options.max_side,
    )
    kwargs = model_load_kwargs(options)
    kwargs["torch_dtype"] = torch.bfloat16
    if quantization == "4bit":
        kwargs.pop("load_in_4bit")
        kwargs.pop("bnb_4bit_quant_type")
        kwargs.pop("bnb_4bit_compute_dtype")
        kwargs["quantization_config"] = transformers.BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    base = transformers.Qwen3VLForConditionalGeneration.from_pretrained(str(snapshot), **kwargs)
    base.requires_grad_(False)
    if quantization == "4bit":
        base = peft.prepare_model_for_kbit_training(base, use_gradient_checkpointing=False)
    targets = [
        name
        for name, _ in base.named_modules()
        if LANGUAGE_PROJECTION.fullmatch(name)
        or (options.vision_projector and VISION_PROJECTOR.fullmatch(name))
    ]
    if not any(LANGUAGE_PROJECTION.fullmatch(name) for name in targets):
        raise ValueError("Qwen language q/v projection modules were not found")
    if options.vision_projector and not any(VISION_PROJECTOR.fullmatch(name) for name in targets):
        raise ValueError("Requested vision projector modules were not found")
    model = peft.get_peft_model(
        base,
        peft.LoraConfig(
            task_type="CAUSAL_LM",
            r=options.rank,
            lora_alpha=options.alpha,
            lora_dropout=0.0,
            target_modules=targets,
            bias="none",
        ),
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    return torch, model, processor, targets, quantization


OOM_RETRIES = 5
OOM_RETRY_WAIT_S = 30.0


def _accumulate_batch(
    torch, model, processor, samples: list[dict], *, options, crop_policy
) -> list[float]:
    """Forward/backward one accumulation batch; returns per-sample losses."""
    batch_losses: list[float] = []
    for sample in samples:
        full, labels, response_tokens = encode_response(
            processor,
            sample,
            max_side=options.max_side,
            max_input_tokens=options.max_input_tokens,
            crop_policy=crop_policy,
        )
        inputs = {name: tensor.to("cuda:0") for name, tensor in full.items()}
        labels = labels.to("cuda:0")
        model.train()
        model.base_model.model.visual.eval()
        output_logits = model(**inputs, use_cache=False, logits_to_keep=response_tokens + 1)
        loss = causal_response_loss(output_logits.logits, labels) / len(samples)
        if not torch.isfinite(loss).item():
            raise ValueError(f"Non-finite loss at {sample['sample_id']}")
        loss.backward()
        batch_losses.append(float(loss.detach()) * len(samples))
    return batch_losses


def _save_checkpoint(torch, output: Path, model, optimizer, state: dict) -> None:
    checkpoint = output / "checkpoint"
    checkpoint.mkdir(exist_ok=True)
    model.save_pretrained(checkpoint, safe_serialization=True)
    torch.save(optimizer.state_dict(), checkpoint / "optimizer.pt")
    temporary = checkpoint / "state.tmp"
    temporary.write_text(
        json.dumps(state, sort_keys=True, indent=2), encoding="utf-8", newline="\n"
    )
    temporary.replace(checkpoint / "state.json")


def resolve_adapter_weights(path: Path) -> Path:
    """Resolve an HF/PEFT adapter directory or direct safetensors file."""
    candidate = path / "adapter_model.safetensors" if path.is_dir() else path
    if not candidate.is_file():
        raise FileNotFoundError(f"adapter weights not found: {candidate}")
    return candidate


def initialize_adapter(model, adapter_path: Path) -> None:
    """Load r12 adapter tensors into a freshly-created, same-rank LoRA model."""
    from peft import set_peft_model_state_dict
    from safetensors.torch import load as load_safetensors

    weights_path = resolve_adapter_weights(adapter_path)
    weights = load_safetensors(weights_path.read_bytes())
    result = set_peft_model_state_dict(model, weights)
    unexpected = getattr(result, "unexpected_keys", None) or []
    # missing_keys covers the whole model, so every frozen base weight shows up
    # there; only a missing LoRA tensor means the adapter does not fit.
    missing = [key for key in (getattr(result, "missing_keys", None) or []) if "lora_" in key]
    del weights
    if unexpected or missing:
        raise ValueError(
            f"initial adapter does not match current LoRA target/rank: "
            f"unexpected={unexpected[:3]}, missing={missing[:3]}"
        )


def run(args, *, backend_factory=make_backend) -> dict:
    options = TrainOptions(
        epochs=args.epochs,
        max_steps=args.max_steps,
        accumulation=args.accumulation,
        rank=args.rank,
        alpha=args.alpha,
        learning_rate=args.learning_rate,
        seed=args.seed,
        max_input_tokens=args.max_input_tokens,
        max_new_tokens=args.max_new_tokens,
        checkpoint_every=args.checkpoint_every,
        max_side=args.max_side,
        quantization=args.quantization,
        vision_projector=args.vision_projector,
    )
    options.validate()
    rows, dataset_sha256 = admit_train_rows(args.dataset)
    crop_policy = dataset_crop_policy(args.dataset, getattr(args, "crop_policy", None))
    balanced = bool(getattr(args, "balance", False))
    if balanced:
        rows = balance_rows(rows, seed=args.seed)
    snapshot = local_snapshot(args.model)
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise FileExistsError(f"Output exists; use --resume: {output}")
    if args.resume and not (output / "checkpoint/state.json").is_file():
        raise FileNotFoundError("--resume requires checkpoint/state.json")
    output.mkdir(parents=True, exist_ok=True)
    torch, model, processor, targets, quantization = backend_factory(snapshot, options)
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=options.learning_rate
    )
    if getattr(args, "init_adapter", None) and not args.resume:
        initialize_adapter(model, Path(args.init_adapter))
    state = {
        "epoch": 0,
        "cursor": 0,
        "optimizer_steps": 0,
        "dataset_sha256": dataset_sha256,
        "options": {**asdict(options), "crop_policy": crop_policy, "balance": balanced},
        "snapshot": str(snapshot),
    }
    if args.resume:
        prior = json.loads((output / "checkpoint/state.json").read_text(encoding="utf-8"))

        # checkpoint_every only changes how often progress is saved, not the training math,
        # so a resume may save more often (e.g. when a shared GPU keeps interrupting it).
        def _training_options(options: dict) -> dict:
            return {k: v for k, v in options.items() if k != "checkpoint_every"}

        if (
            prior["dataset_sha256"] != state["dataset_sha256"]
            or prior["snapshot"] != state["snapshot"]
            or _training_options(prior["options"]) != _training_options(state["options"])
        ):
            raise ValueError("Resume dataset, model or options differ from checkpoint")
        # Load the checkpoint into the default adapter that training updates and saves.
        # (An earlier version trained a second "resume" adapter, saved under resume/,
        # while the default adapter stayed at its zero init; prefer those weights.)
        from peft import set_peft_model_state_dict
        from safetensors.torch import load as load_safetensors

        legacy = output / "checkpoint/resume/adapter_model.safetensors"
        source = legacy if legacy.is_file() else output / "checkpoint/adapter_model.safetensors"
        # Read into memory: a memory-mapped load keeps the file mapped on Windows and the
        # next checkpoint save over the same path then fails (OS error 1224).
        weights = load_safetensors(source.read_bytes())
        result = set_peft_model_state_dict(model, weights)
        del weights
        if getattr(result, "unexpected_keys", None):
            raise ValueError(
                f"checkpoint keys do not match the adapter: {result.unexpected_keys[:3]}"
            )
        optimizer.load_state_dict(
            torch.load(output / "checkpoint/optimizer.pt", map_location="cpu", weights_only=True)
        )
        state = prior
    started = time.perf_counter()
    losses: list[float] = []
    log_path = output / "training-log.jsonl"
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        for epoch in range(state["epoch"], options.epochs):
            order = list(range(len(rows)))
            random.Random(options.seed + epoch).shuffle(order)
            cursor = state["cursor"] if epoch == state["epoch"] else 0
            while cursor < len(order):
                end = min(cursor + options.accumulation, len(order))
                batch_losses = None
                for attempt in range(OOM_RETRIES + 1):
                    optimizer.zero_grad(set_to_none=True)
                    try:
                        batch_losses = _accumulate_batch(
                            torch,
                            model,
                            processor,
                            [rows[i] for i in order[cursor:end]],
                            options=options,
                            crop_policy=crop_policy,
                        )
                        break
                    except RuntimeError as error:
                        # The GPU is shared with other jobs; a transient out-of-memory must not
                        # end a multi-hour run. The batch is recomputed from zeroed gradients,
                        # so a retried step is identical to an uninterrupted one.
                        if "out of memory" not in str(error):
                            raise
                        optimizer.zero_grad(set_to_none=True)
                        if not isinstance(error, torch.OutOfMemoryError):
                            raise  # driver-level OOM: the CUDA context is unusable; resume.
                        torch.cuda.empty_cache()
                        if attempt == OOM_RETRIES:
                            break
                        print(
                            f"CUDA out of memory at step {state['optimizer_steps'] + 1}; "
                            f"retry {attempt + 1}/{OOM_RETRIES}",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(OOM_RETRY_WAIT_S)
                if batch_losses is None:
                    # This batch never fits under the allocator cap: skip it (recorded in the
                    # log and the report) rather than lose the run.
                    skipped = [rows[i]["sample_id"] for i in order[cursor:end]]
                    state.setdefault("skipped_samples", []).extend(skipped)
                    cursor = end
                    state.update(epoch=epoch, cursor=cursor)
                    log.write(
                        json.dumps(
                            {"epoch": epoch + 1, "cursor": cursor, "skipped_oom": skipped},
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    log.flush()
                    continue
                losses.extend(batch_losses)
                torch.nn.utils.clip_grad_norm_(
                    [p for p in model.parameters() if p.requires_grad], 1.0, error_if_nonfinite=True
                )
                optimizer.step()
                state["optimizer_steps"] += 1
                cursor = end
                state.update(epoch=epoch, cursor=cursor)
                entry = {
                    "epoch": epoch + 1,
                    "cursor": cursor,
                    "optimizer_step": state["optimizer_steps"],
                    "loss": round(sum(batch_losses) / len(batch_losses), 6),
                }
                log.write(json.dumps(entry, sort_keys=True) + "\n")
                log.flush()
                if state["optimizer_steps"] % options.checkpoint_every == 0:
                    _save_checkpoint(torch, output, model, optimizer, state)
                if options.max_steps and state["optimizer_steps"] >= options.max_steps:
                    break
            if cursor == len(order):
                state.update(epoch=epoch + 1, cursor=0)
            _save_checkpoint(torch, output, model, optimizer, state)
            if options.max_steps and state["optimizer_steps"] >= options.max_steps:
                break
    completed = state["epoch"] >= options.epochs
    lora_b_mean_abs = None
    if completed:
        model.save_pretrained(output / "adapter", safe_serialization=True)
        saved_path = output / "adapter/adapter_model.safetensors"
        if saved_path.is_file():  # mock backends in tests write no weights
            from safetensors.torch import load_file

            saved = load_file(str(saved_path))
            b_weights = [tensor for key, tensor in saved.items() if "lora_B" in key]
            lora_b_mean_abs = float(sum(t.abs().mean() for t in b_weights) / max(1, len(b_weights)))
        if state["optimizer_steps"] > 0 and lora_b_mean_abs == 0.0:
            # LoRA B starts at zero: an all-zero saved adapter means the trained
            # weights were not the ones saved (it would merge into the base model).
            raise RuntimeError("saved adapter has all-zero lora_B weights after training")
    report = {
        "schema_version": 1,
        "dataset_sha256": dataset_sha256,
        "train_samples": len(rows),
        "train_worlds": len({row["world"] for row in rows}),
        "snapshot": str(snapshot),
        "options": {**asdict(options), "crop_policy": crop_policy, "balance": balanced},
        "quantization": quantization,
        "optimizer_stream_decision_counts": {
            name: sum(
                row["assistant_target"].get("decision", "mission_plan") == name for row in rows
            )
            for name in ("confirm_target", "approach_track", "reject_track")
        },
        "target_modules": targets,
        "optimizer_steps": state["optimizer_steps"],
        "completed": completed,
        "elapsed_s_this_run": round(time.perf_counter() - started, 3),
        "mean_loss_this_run": sum(losses) / len(losses) if losses else None,
        "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
        "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        "adapter_path": str(output / "adapter") if completed else None,
        "lora_b_mean_abs": lora_b_mean_abs,
        "skipped_oom_samples": state.get("skipped_samples", []),
    }
    (output / "training-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/models/slow-system-lora-r1"
    )
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument(
        "--max-steps", type=int, default=0, help="Total optimizer step cap; 0 means full epochs"
    )
    parser.add_argument("--accumulation", type=int, default=4)
    parser.add_argument("--rank", type=int, default=4)
    parser.add_argument("--alpha", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--max-input-tokens", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--checkpoint-every", type=int, default=50)
    parser.add_argument("--max-side", type=int, default=448)
    parser.add_argument("--quantization", choices=("auto", "4bit", "bf16"), default="auto")
    parser.add_argument("--vision-projector", action="store_true")
    parser.add_argument("--crop-policy", choices=("none", "candidate_zoom_v1", "candidate_zoom_v2"))
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--init-adapter",
        type=Path,
        default=None,
        help="initialize LoRA weights from an existing PEFT adapter; optimizer starts fresh",
    )
    args = parser.parse_args(argv)
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
