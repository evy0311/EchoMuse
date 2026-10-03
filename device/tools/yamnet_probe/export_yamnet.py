#!/usr/bin/env python3
"""Build EchoMuse's neural-only YAMNet ONNX asset from official sources.

Run on a workstation, never on the Echo. Recommended isolated environment:

  python -m venv .venv
  .venv/bin/pip install tensorflow-cpu==2.15.1 tf-keras==2.15.0 tf2onnx==1.16.1
  .venv/bin/python export_yamnet.py --output-dir ./alarm-assets

Every downloaded input is pinned and hash checked. The resulting graph takes
one [1, 96, 64] YAMNet log-mel patch and returns [1, 521] scores. Firmware
implements the official waveform frontend in Go because the full waveform
graph is not viable on the Echo's ARMv7 ONNX Runtime build.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import sys
import urllib.request
from pathlib import Path

MODEL_COMMIT = "930a6f98f7debcc32ca7afbca4a176dbf9211e03"
BASE = ("https://raw.githubusercontent.com/tensorflow/models/" + MODEL_COMMIT
        + "/research/audioset/yamnet/")
WEIGHTS_URL = "https://storage.googleapis.com/audioset/yamnet.h5"
WEIGHTS_SHA256 = "13c3308955bbfaef262f175ac9c40e47b134573a93984f009220dd7cc12a1744"
CLASS_MAP_SHA256 = "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"


def fetch(url: str, path: Path, sha256: str | None = None) -> None:
    if not path.exists():
        urllib.request.urlretrieve(url, path)
    if sha256:
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != sha256:
            raise SystemExit(f"{path}: sha256 {got}, want {sha256}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True, type=Path)
    args = ap.parse_args()
    out = args.output_dir.resolve()
    src = out / "source"
    src.mkdir(parents=True, exist_ok=True)
    for name in ("features.py", "params.py", "yamnet.py"):
        fetch(BASE + name, src / name)
    fetch(WEIGHTS_URL, src / "yamnet.h5", WEIGHTS_SHA256)
    fetch(BASE + "yamnet_class_map.csv", out / "yamnet_class_map.csv",
          CLASS_MAP_SHA256)

    import tensorflow as tf
    import tf2onnx

    sys.path.insert(0, str(src))
    params = importlib.import_module("params")
    yamnet = importlib.import_module("yamnet")
    model_params = params.Params()
    full_model = yamnet.yamnet_frames_model(model_params)
    full_model.load_weights(str(src / "yamnet.h5"))

    tf_keras = importlib.import_module("tf_keras")
    # Reset Keras' generated-name counters so the standalone classifier layers
    # have the same names as the corresponding layers in the loaded model.
    tf_keras.backend.clear_session()
    log_mel = tf_keras.layers.Input(batch_shape=(1, 96, 64), dtype=tf.float32,
                                    name="log_mel")
    scores, _ = yamnet.yamnet(log_mel, model_params)
    classifier = tf_keras.Model(log_mel, scores,
                                name="echomuse_yamnet_classifier")
    for layer in classifier.layers:
        if layer.weights:
            layer.set_weights(full_model.get_layer(layer.name).get_weights())

    tf2onnx.convert.from_keras(
        classifier,
        input_signature=[tf.TensorSpec([1, 96, 64], tf.float32,
                                       name="log_mel")],
        opset=17,
        output_path=str(out / "yamnet_classifier.onnx"),
    )
    print(out / "yamnet_classifier.onnx")


if __name__ == "__main__":
    main()
