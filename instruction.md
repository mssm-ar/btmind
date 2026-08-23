**Caution**
Follow this istruction.
Set 
neurons/generator/services/openrouter_service.py
neurons/generator/miner.py
Do with cursor to try see logs of model.
Do not forget to set this model at neurons/generator/services/openrouter_service.py and env
google/veo-3.1-lite
google/gemini-2.5-flash-image

# GAS Subnet (SN34) — Generative Mining Setup Guide

This guide walks through environment installation and running a **generative miner** on
Bittensor Subnet 34 (GAS). Generative miners run a FastAPI server that receives prompts
from validators and returns AI-generated images/video via third-party generation APIs
(OpenAI, OpenRouter, Stability AI, Runway).

---

## 1. Prerequisites

- **Python 3.10+**
- **Git**
- **uv** (fast Python package manager)
- A **Bittensor wallet** (coldkey + hotkey) registered on the subnet
- At least one API key from a supported generation provider (see [Section 4](#4-choose-a-generation-provider--get-api-keys))

### Install `uv`

```bash
# Option 1: official installer (recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Option 2: via pip
pip install uv
```

---

## 2. Clone the Repository & Install Dependencies

```bash
git clone https://github.com/BitMind-AI/bitmind-subnet.git
cd bitmind-subnet
./install.sh
```

The install script will:
- Verify Python 3.10+ and `uv` are available
- Install system dependencies: `pkg-config`, `cmake`, `ffmpeg`, Node.js, `npm`, `pm2`, `dotenv`
- Create a `.venv` virtual environment via `uv` and install all Python dependencies
- Install the `gascli` command-line tool

Activate the virtual environment before using `gascli`:

```bash
source .venv/bin/activate
```

> If you only plan to run a generative miner (no local model training), the full install
> (without `--no-system-deps`) is required since `pm2`/Node.js are used to manage the
> miner process.

### Troubleshooting: `flash-attn` build failure on machines without a GPU

`./install.sh` runs `uv sync`, which installs `flash-attn` unconditionally (it's a hard
dependency in `pyproject.toml`). `flash-attn` compiles CUDA kernels at install time via
`nvcc`, so **on any machine without an NVIDIA GPU/CUDA toolkit, this build fails** with an
error like:

```
OSError: CUDA_HOME environment variable is not set. Please set it to your CUDA install root.
```

`flash_attn` is only ever imported by validator-side prompt-generation code, wrapped in a
`try/except ImportError` — it is **not required** to run a generative miner. If you hit
this error, install everything else while explicitly skipping it:

```bash
# Instead of `uv sync`, run:
uv sync --no-install-package flash-attn

# Re-link the editable gas package without re-triggering dependency resolution:
uv pip install -e . --no-deps

# Install the remaining git dependencies (pure Python, no CUDA needed):
source .venv/bin/activate
uv pip install git+https://github.com/deepseek-ai/Janus.git
uv pip install git+https://github.com/openai/CLIP.git
```

Then verify everything imports cleanly:

```bash
.venv/bin/python -c "import neurons.generator.miner; print('OK')"
.venv/bin/gascli --help
```

---

## 3. Set Up Your Bittensor Wallet

If you don't already have a wallet, create one with `btcli` (installed as a dependency):

```bash
# Create a coldkey (holds funds/registration)
btcli wallet new_coldkey --wallet.name miner1

# Create a hotkey (used to run the miner)
btcli wallet new_hotkey --wallet.name miner1 --wallet.hotkey default
```

Register your hotkey on the subnet (requires TAO for registration cost):

```bash
# Mainnet (netuid 34)
btcli subnet register \
  --wallet.name E \
  --wallet.hotkey E_03 \
  --netuid 34 \
  --subtensor.chain_endpoint wss://entrypoint-finney.opentensor.ai:443

# Testnet (netuid 379) — recommended for first-time setup / testing
btcli subnet register \
  --wallet.name miner1 \
  --wallet.hotkey default \
  --netuid 379 \
  --subtensor.chain_endpoint wss://test.finney.opentensor.ai:443
```

---

## 4. Choose a Generation Provider & Get API Keys

You must configure **at least one** provider per modality (image and/or video) you want
to serve. All generated content must carry a valid **C2PA signature** from a trusted
signer, or validators will reject it — this restricts which underlying models are usable
per provider.

| Provider | Env Var | Modalities | Notes |
|---|---|---|---|
| **OpenRouter** (recommended) | `OPEN_ROUTER_API_KEY` | Image, Video | Default image model `google/gemini-3-pro-image-preview`. Video limited to C2PA-capable models: Google Veo (`google/veo-3.1`, `-fast`, `-lite`) and ByteDance Seedance (`bytedance/seedance-2.0`, `-fast`). Get a key at [openrouter.ai/keys](https://openrouter.ai/keys). |
| **OpenAI** | `OPENAI_API_KEY` | Image, Video | DALL-E 3 (image), Sora 2 (video — being deprecated by OpenAI, shutdown 2026-09-24). Get a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). |
| **Stability AI** | `STABILITY_API_KEY` | Image | Stable Diffusion 3.5 / Stable Image Ultra / Core. Get a key at [stability.ai](https://stability.ai). |
| **Runway** | `RUNWAYML_API_KEY` | Video | `gen4.5`, `veo3.1`, `veo3.1_fast`, `veo3`. Get a key at [app.runwayml.com](https://app.runwayml.com/). |

**Practical tip (cost/reward tradeoff):** `google/veo-3.1-lite` via OpenRouter is the
cheapest C2PA-capable video model and sets the reward baseline (1.0x multiplier).
`bytedance/seedance-2.0-fast` costs ~4x more but earns a 2x multiplier — a strong
efficiency tradeoff if you want higher per-sample rewards.

---

## 5. Configure Environment Variables

Copy the template and edit it:

```bash
cp .env.gen_miner.template .env.gen_miner
```

Edit `.env.gen_miner` with your settings:

sudo ufw allow 9001

```bash
# --- Which service handles each modality ---
IMAGE_SERVICE=openrouter    # openai, openrouter, stabilityai, or none
VIDEO_SERVICE=openrouter    # openai, openrouter, stabilityai, runway, or none

# --- API keys (fill in the ones you're using) ---
OPENAI_API_KEY=
OPEN_ROUTER_API_KEY=your_key_here
STABILITY_API_KEY=
RUNWAYML_API_KEY=
HUGGINGFACE_HUB_TOKEN=

# --- Bittensor network ---
BT_NETUID=379                                          # 34 = mainnet, 379 = testnet
BT_CHAIN_ENDPOINT=wss://test.finney.opentensor.ai:443  # mainnet: wss://entrypoint-finney.opentensor.ai:443

# --- Wallet ---
BT_WALLET_NAME=miner1
BT_WALLET_HOTKEY=default

# --- Axon (your miner's public endpoint) ---
BT_AXON_PORT=9001
BT_AXON_IP=0.0.0.0
# BT_AXON_EXTERNAL_IP=your.public.ip   # uncomment/set if behind NAT

# --- Miner performance settings ---
MINER_DEVICE=auto
MINER_MAX_CONCURRENT_TASKS=5
MINER_TASK_TIMEOUT=300
MINER_SAVE_LOCALLY=true
MINER_OUTPUT_DIR=./miner_generated_content

# --- Logging ---
BT_LOGGING_LEVEL=INFO
```

> Switch `BT_NETUID`/`BT_CHAIN_ENDPOINT` to mainnet values once you've validated your
> setup works correctly on testnet.

---

## 6. Run the Generative Miner

### Using `gascli` (recommended)

```bash
source .venv/bin/activate

# Start the miner
gascli generator start
# aliases: gascli gen start / gascli g start

# Check status
gascli generator status

# View logs
gascli generator logs
gascli generator logs --follow   # tail logs live

# Show config + API key status
gascli generator info

# Stop / restart / delete
gascli generator stop
gascli generator restart
gascli generator delete
```

### Using PM2 directly (no venv activation required)

```bash
pm2 start gen_miner.config.js
pm2 logs bitmind-generative-miner
pm2 status
```

---

## 7. Verify It's Working

Your miner exposes these HTTP endpoints for validators to call:

- `GET /health` — health check
- `GET /miner_info` — capabilities/info
- `POST /gen_image` — image generation requests
- `POST /gen_video` — video generation requests
- `GET /status/{task_id}` — task status polling

Locally test a hop of the C2PA verification logic validators use, against any file you
generate:

```bash
gascli generator verify-c2pa <path/to/generated_file> --verbose
```

If a generation fails C2PA verification, double check that:
- The provider/model you selected is on the C2PA-capable allowlist (see Section 4)
- Your API key is valid and has quota
- You haven't accidentally routed video requests to an excluded model (e.g. Kling, Wan,
  Hailuo, or Sora via OpenRouter — none of these produce valid C2PA manifests)

---

## 8. How Rewards Work (quick recap)

- **Base reward** = validation pass rate × min(verified samples, 10) — rewards passing
  C2PA/prompt-alignment checks, with a rampup bonus for your first 10 samples
- **Fool-rate multiplier** (0–2x) = rewarded for generations that fool discriminative
  miners, scaled by your evaluation sample volume
- **Model-cost multiplier** = `sqrt(model_price / baseline_price)` — pricier/higher-quality
  models earn more per sample (capped so it's not linear)
- Generative mining currently receives **16%** of total subnet emission, split
  proportionally across all active generators (not winner-take-all)

See `docs/Incentive.md` and `docs/Generative-Mining.md` in this repo for full formulas.

---

## Troubleshooting

- **Miner won't start** — check `gascli generator logs` for stack traces; confirm your
  `.env.gen_miner` file exists and `BT_WALLET_NAME`/`BT_WALLET_HOTKEY` match a registered
  hotkey.
- **No API key configured for a modality** — set `IMAGE_SERVICE`/`VIDEO_SERVICE=none` to
  explicitly disable a modality you don't want to serve, otherwise unconfigured services
  will reject requests.
- **Port already in use** — change `BT_AXON_PORT` in `.env.gen_miner`.
- **Behind NAT / cloud VM** — set `BT_AXON_EXTERNAL_IP` to your public-facing IP so
  validators can reach your axon.
