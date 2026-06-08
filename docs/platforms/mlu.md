# Cambricon MLU Backend

This repository includes an in-tree MLU backend for SRT. The initial target is
serving dense text-generation models such as Qwen3-14B with MLU graph enabled.

## Environment

Use a Cambricon PyTorch environment that provides the MLU runtime stack
(`torch_mlu`, `torch_mlu_ops`, CNCL, and the matching driver/runtime libraries).
The MLU Python stack is provided by that environment rather than by SGLang's
CUDA-oriented default dependencies.

Install SGLang from this checkout with the MLU-specific pyproject:

```bash
cp python/pyproject.toml /tmp/sglang-pyproject.toml.bak
cp python/pyproject_mlu.toml python/pyproject.toml
python -m pip install -e "python[srt_mlu]" --no-build-isolation
cp /tmp/sglang-pyproject.toml.bak python/pyproject.toml
```

`python/pyproject_mlu.toml` keeps CUDA-only SGLang dependencies out of the MLU
install and leaves the Cambricon runtime packages to the active environment.

## Validation

Check that the MLU runtime is available:

```bash
python - <<'PY'
import torch
import torch_mlu  # noqa: F401

print(torch.mlu.is_available(), torch.mlu.device_count(), torch.mlu.current_device())
PY
```

Run Qwen3-14B with MLU graph enabled:

```bash
python -m sglang.launch_server \
  --model-path /data/models/Qwen3-14B/ \
  --device mlu \
  --trust-remote-code \
  --host 127.0.0.1 \
  --port 30000 \
  --skip-server-warmup
```

Send a smoke request:

```bash
curl http://127.0.0.1:30000/generate \
  -H "Content-Type: application/json" \
  -d '{"text":"The future of AI is","sampling_params":{"max_new_tokens":16,"temperature":0}}'
```

Run the registered Qwen3-14B E2E smoke test. It starts the server, waits for
readiness, sends one single request plus four concurrent requests with different
prompts, and checks the server log for MLU graph capture evidence:

```bash
python -m unittest discover \
  -s test/registered/mlu \
  -p 'test_mlu_qwen3_14b_e2e.py'
```
