# Copyright 2023-2026 SGLang Team
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Torch probability filtering for eager MLU speculative verification."""

import torch


def top_k_renorm_prob(probs: torch.Tensor, top_ks: torch.Tensor) -> torch.Tensor:
    """Torch fallback for per-row top-k filtering and renormalization."""
    vocab_size = probs.shape[-1]
    top_ks = top_ks.reshape(-1).to(device=probs.device, dtype=torch.long)
    top_ks = top_ks.clamp(min=1, max=vocab_size)
    sorted_probs, sorted_indices = probs.sort(dim=-1, descending=True)
    ranks = torch.arange(vocab_size, device=probs.device)
    sorted_probs.masked_fill_(ranks.unsqueeze(0) >= top_ks.unsqueeze(1), 0)
    sorted_probs.div_(sorted_probs.sum(dim=-1, keepdim=True).clamp_min(1e-20))
    return torch.zeros_like(probs).scatter_(1, sorted_indices, sorted_probs)


def top_p_renorm_prob(probs: torch.Tensor, top_ps: torch.Tensor) -> torch.Tensor:
    """Torch fallback for per-row nucleus filtering and renormalization."""
    top_ps = top_ps.reshape(-1).to(device=probs.device, dtype=probs.dtype)
    sorted_probs, sorted_indices = probs.sort(dim=-1, descending=True)
    cumulative = sorted_probs.cumsum(dim=-1)
    sorted_probs.masked_fill_(
        cumulative - sorted_probs > top_ps.unsqueeze(1),
        0,
    )
    sorted_probs.div_(sorted_probs.sum(dim=-1, keepdim=True).clamp_min(1e-20))
    return torch.zeros_like(probs).scatter_(1, sorted_indices, sorted_probs)
