# Wraps the vendored Hermes image so neutts (local, offline TTS) + its espeak-ng
# phonemizer dependency survive a future `docker compose pull`/recreate - a bare
# `docker exec pip install` would be lost the moment the base image is re-pulled.
FROM nousresearch/hermes-agent:latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Same install path Hermes's own setup wizard uses for local TTS engines (uv targeting
# the app's venv directly, since that venv may not have pip seeded - see _pip_install
# in hermes_cli/tools_config_cua.py).
RUN VIRTUAL_ENV=/opt/hermes/.venv uv pip install 'neutts[all]'

# neutts[all] pulls torchao==0.18.0, but torchtune 0.6.1 (one of its own transitive deps,
# via neucodec -> torchtune.modules.RotaryPositionalEmbeddings) imports
# torchao.dtypes.nf4tensor, which 0.18.0 no longer has (`import neutts` crashes with
# ModuleNotFoundError). Pin back to a torchao release that still ships it - confirmed
# working live against this exact torch/torchtune pairing.
RUN VIRTUAL_ENV=/opt/hermes/.venv uv pip install 'torchao==0.9.0'

# Piper (VITS-based, used in Home Assistant Voice) - fully standalone, no gated
# model/HF_TOKEN needed unlike neutts above. Installed alongside neutts (not instead
# of) so tts.provider can be flipped back later without another image rebuild.
RUN VIRTUAL_ENV=/opt/hermes/.venv uv pip install 'piper-tts'
