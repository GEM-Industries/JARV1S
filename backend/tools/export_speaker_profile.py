"""Export a speaker profile gallery for wakeword evaluation / offline tooling.

Product runtime uses owner profiles under DATA_DIR. This tool remains for
developer/eval workflows that build a gallery from a training manifest.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.voice.speaker_verifier import (  # noqa: E402
    build_profile_from_manifest,
    load_speaker_extractor,
    save_speaker_profile,
    speaker_model_id,
)

DEFAULT_MODEL = BACKEND_DIR / "resources/models/speaker/nemo_en_titanet_small.onnx"
DEFAULT_MANIFEST = REPO_ROOT / "training/wakeword/manifests/speaker_enrollment.jsonl"
DEFAULT_OUTPUT = BACKEND_DIR / "resources/models/speaker/eval_profile.npz"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export wakeword speaker profile artifact")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--split", default="enroll")
    parser.add_argument("--num-threads", type=int, default=1)
    args = parser.parse_args()

    extractor = load_speaker_extractor(args.model, num_threads=args.num_threads)
    profile = build_profile_from_manifest(extractor, args.manifest, split=args.split)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_speaker_profile(
        args.output,
        model_id=speaker_model_id(args.model),
        embeddings=profile,
    )
    print(f"Wrote {args.output} shape={profile.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
