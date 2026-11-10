# NOTICE

Video X-Ray — Video Analysis Workbench plugin for Hermes Desktop
Copyright 2026 LuuMoss (Xiaoya)

This product includes software developed by LuuMoss (Xiaoya), licensed under the
Apache License, Version 2.0 (see `LICENSE`).

## Third-party attribution (not distributed by this repository)

This plugin **does not bundle or redistribute** any of the following binaries or
weights. They are downloaded and installed by the end user; listing them here is
attribution, not redistribution.

| Component | Source | License | Role |
|---|---|---|---|
| Confucius4-R2T2 engine code | [netease-youdao/Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2) | Apache-2.0 | speech transcription engine (the `r2t2-runtime/core/` adapters in this repo are derived from it, with small patches by Xiaoya) |
| Confucius4-R2T2 weights | [netease-youdao/Confucius4-R2T2-GGUF](https://huggingface.co/netease-youdao/Confucius4-R2T2-GGUF) | NetEase Youdao Model Use License | model weights, downloaded by the user |
| llama.cpp (ggml) | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | inference backend inside the R2T2 native runtime (pinned upstream commit `ad6c668`) |
| Windows native runtime (CPU/CUDA builds of the R2T2 engine) | [T8mars/Strata-T8](https://github.com/T8mars/Strata-T8) · [T8mars/comfyui-confucius-r2t2-t8](https://github.com/T8mars/comfyui-confucius-r2t2-t8) | MIT (pack) / Apache-2.0 (follows upstream) | optional runtime the user downloads; this plugin only invokes it out-of-process |
| FireRedVAD | [FireRedTeam/FireRedVAD](https://github.com/FireRedTeam/FireRedVAD) | Apache-2.0 | voice-activity-detection ONNX model shipped alongside the weights |
| faster-whisper (optional) | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | alternative local transcription engine, installed by the user via pip |

The plugin process communicates with the R2T2 runtime exclusively over a child
process pipe (stdin WAV → stdout JSON). No third-party code is statically or
dynamically linked into this plugin's own code.

## MiniMax-H3 prompt format note

The "reverse prompt" export targets the `integrated_multimodal_description`
prompt style of MiniMax-H3. It is plain text output; MiniMax trademarks belong
to their owner and are referenced descriptively only.
