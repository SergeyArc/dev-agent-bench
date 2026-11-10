# Serving the four models

These scripts start [vLLM](https://github.com/vllm-project/vllm) `v0.30.0` for the models used in the matrix. Paths and host details are placeholders — adapt them to your machine. Set `GPU_BUS_RE` (PCI bus_id regex for `nvidia-smi`) and `NVIDIA_VISIBLE_DEVICES` before launching a script. Pin revisions and measured settings live in `models.lock.json`.

## Models

| Key (`--model` in the runner) | Weights | Notes |
|---|---|---|
| `qwen3.8-27b` | Qwen3.8-27B (BF16) | tool parser `qwen3_coder`, reasoning parser `qwen3` |
| `gemma-4-31b` | Gemma 4 31B-it (BF16) | tool parser `gemma4`, reasoning parser `gemma4`; attention backend `TRITON_ATTN` |
| `nemotron-3-super-120b-a12b` | Nemotron 3 Super (FP8) | `--trust-remote-code`, Mamba cache `float32` |
| `devstral-2-123b` | Devstral 2 123B (FP8) | `--tokenizer-mode mistral`, tool parser `mistral` |

## Profiles and context limits

Shared for every model: `--gpu-memory-utilization 0.95`, `--max-num-seqs 4`. Set `TP` to the tensor-parallel size that fits your GPUs.

| Profile | `max_model_len` | KV cache dtype |
|---|---|---|
| P1 (default in the runner) | 131072 | fp8 |
| P2 | 65536 | bfloat16 |
| P2p | 65536 | fp8 |

The matrix used P1. Launch:

```bash
./serving/qwen3.8.sh P1
./serving/gemma-4.sh P1
./serving/nemotron-3.sh P1
./serving/devstral.sh P1
```

`smoke.py` checks the OpenAI-compatible endpoint after start (API key from `/run/api_key`).
