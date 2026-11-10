#!/usr/bin/env bash
# Nemotron 3 Super 120B-A12B (FP8, hybrid Mamba-Transformer MoE) — vLLM v0.30.0 on GPUs selected via NVIDIA_VISIBLE_DEVICES.
# Start: ./nemotron-3.sh P1|P2|P2p   Stop: docker stop bench-vllm-nemotron-3
set -euo pipefail

# ---------- model-specific part ----------
NAME=bench-vllm-nemotron-3
SNAP=/hf/hub/models--nvidia--NVIDIA-Nemotron-3-Super-120B-A12B-FP8/snapshots/744b1880a37996c5d56bf454ae164dfd74d77c4e
MODEL_ARGS=(
  --served-model-name nemotron-3-super
  # From the model card serve command: config code in the snapshot and fp32 for Mamba state.
  # --max-cudagraph-capture-size 128, --swap-space 0 — conflict with the shared profile or have no effect.
  --trust-remote-code
  --mamba-ssm-cache-dtype float32
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
  --reasoning-parser nemotron_v3
  --default-chat-template-kwargs '{"enable_thinking": true}'
  --override-generation-config '{"temperature": 1.0, "top_p": 0.95}'
)

# FlashInfer autotune cache is removed before start; leaving it in place can hang the server.
rm -rf /path/to/dev-agent-bench/cache/.cache/vllm/flashinfer_autotune_cache

# ---------- shared part (same in every serving/<model>.sh) ----------
PROFILE=${1:?profile: P1|P2|P2p}
case $PROFILE in
  P1)  MML=131072; KV=fp8 ;;
  P2)  MML=65536;  KV=bfloat16 ;;  # explicit: for an fp8 checkpoint (Nemotron) auto resolves to fp8_e4m3
  P2p) MML=65536;  KV=fp8 ;;
  *)   echo "profile: P1|P2|P2p" >&2; exit 2 ;;
esac
IMAGE=vllm/vllm-openai:v0.30.0
B=/path/to/dev-agent-bench
PORT=8300

: "${GPU_BUS_RE:?set GPU_BUS_RE to a nvidia-smi PCI bus_id regex}"
: "${NVIDIA_VISIBLE_DEVICES:?set NVIDIA_VISIBLE_DEVICES to the GPUs for this run}"
: "${TP:?set TP to the tensor-parallel size for your GPUs}"

busy=$(nvidia-smi --query-compute-apps=gpu_bus_id,pid,used_memory --format=csv,noheader | grep -iE "$GPU_BUS_RE" || true)
if [ -n "$busy" ]; then echo "GPUs <gpu-ids> busy with processes: $busy" >&2; exit 1; fi
mem=$(nvidia-smi --query-gpu=pci.bus_id,memory.used --format=csv,noheader,nounits | grep -iE "$GPU_BUS_RE" | awk -F', ' '$2 > 1024')
if [ -n "$mem" ]; then echo "GPUs <gpu-ids> have used memory (MiB): $mem" >&2; exit 1; fi
echo "GPUs <gpu-ids> free: $(date -Is)"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "container $NAME already exists — docker rm $NAME after saving the log" >&2; exit 1
fi

mkdir -p "$B/cache" "$B/logs"
KEY=$B/serving/.api_key
if [ ! -s "$KEY" ]; then (umask 077; openssl rand -hex 24 > "$KEY"); fi

L=/usr/lib/x86_64-linux-gnu; V=595.71.05; D=/usr/local/nvidia/lib64
LIBS=(-v /usr/bin/nvidia-smi:/usr/local/nvidia/bin/nvidia-smi:ro)
for n in libcuda.so.1 libcuda.so libcuda.so.$V; do LIBS+=(-v $L/libcuda.so.$V:$D/$n:ro); done
for n in libnvidia-ml.so.1 libnvidia-ml.so libnvidia-ml.so.$V; do LIBS+=(-v $L/libnvidia-ml.so.$V:$D/$n:ro); done
for n in libnvidia-nvvm.so.4 libnvidia-nvvm.so libnvidia-nvvm.so.$V; do LIBS+=(-v $L/libnvidia-nvvm.so.$V:$D/$n:ro); done
for n in libnvidia-ptxjitcompiler.so.1 libnvidia-ptxjitcompiler.so libnvidia-ptxjitcompiler.so.$V; do
  LIBS+=(-v $L/libnvidia-ptxjitcompiler.so.$V:$D/$n:ro); done
LIBS+=(-v $L/libnvidia-gpucomp.so.$V:$D/libnvidia-gpucomp.so.$V:ro)

docker run -d --name "$NAME" \
  --gpus "device=${NVIDIA_VISIBLE_DEVICES}" \
  "${LIBS[@]}" \
  --shm-size 16g \
  --user "$(id -u):$(id -g)" -e HOME=/cache -e HF_HOME=/hf -e HF_HUB_OFFLINE=1 -e HF_MODULES_CACHE=/cache/hf_modules \
  -v "$B/hf:/hf:ro" -v "$B/cache:/cache" -v "$KEY:/run/api_key:ro" \
  -p "127.0.0.1:$PORT:8000" \
  --label "bench.profile=$PROFILE" \
  --entrypoint sh "$IMAGE" -c 'export VLLM_API_KEY="$(cat /run/api_key)"; exec vllm serve "$@"' vllm \
  "$SNAP" \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size "$TP" --max-model-len "$MML" --kv-cache-dtype "$KV" \
  --gpu-memory-utilization 0.95 --max-num-seqs 4 \
  "${MODEL_ARGS[@]}"

echo "started $NAME profile $PROFILE (max_model_len=$MML kv=$KV) on 127.0.0.1:$PORT"
