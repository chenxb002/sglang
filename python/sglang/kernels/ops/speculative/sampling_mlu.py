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
"""Eager target-only sampling for MLU topk=1 linear chains.

Uses torch operations because the CUDA target-only sampler is unavailable on MLU.
The retrieval indices must describe a contiguous linear chain.
"""

import torch


def chain_speculative_sampling_target_only(
    predicts: torch.Tensor,
    accept_index: torch.Tensor,
    accept_token_num: torch.Tensor,
    candidates: torch.Tensor,
    retrive_index: torch.Tensor,
    uniform_samples: torch.Tensor,
    uniform_samples_for_final_sampling: torch.Tensor,
    target_probs: torch.Tensor,
    retrive_next_token: torch.Tensor,
    retrive_next_sibling: torch.Tensor,
    draft_probs: torch.Tensor,
    deterministic: bool = True,
    threshold_single: float = 1.0,
    threshold_acc: float = 1.0,
) -> None:
    """Target-only sampler for a topk=1 EAGLE linear chain."""
    batch_size, num_slots = candidates.shape
    if (
        accept_index.shape != candidates.shape
        or retrive_index.shape != candidates.shape
    ):
        raise ValueError(
            "MLU target-only sampling requires candidates, retrieve_index, and "
            "accept_index to have identical linear-chain shapes."
        )

    batch_indices = torch.arange(batch_size, device=candidates.device)
    num_drafts = num_slots - 1
    if num_drafts:
        candidate_tokens = candidates[:, 1:].to(torch.long)
        proposal_probs = (
            target_probs[:, :-1]
            .gather(dim=-1, index=candidate_tokens.unsqueeze(-1))
            .squeeze(-1)
        )
        # Match CUDA target-only verification: masked tokens must never be
        # accepted, even with a zero coin or a relaxed threshold_single=0.
        correct = (proposal_probs > 0) & (
            (
                uniform_samples[:, :num_drafts]
                < proposal_probs / max(float(threshold_acc), 1e-9)
            )
            | (proposal_probs >= float(threshold_single))
        )
        correct = (
            correct.to(torch.int32).cumprod(dim=-1, dtype=torch.int32).to(torch.bool)
        )
        num_correct_drafts = correct.sum(dim=-1, dtype=torch.long)
    else:
        candidate_tokens = candidates.new_empty((batch_size, 0), dtype=torch.long)
        proposal_probs = target_probs.new_empty((batch_size, 0))
        correct = candidates.new_empty((batch_size, 0), dtype=torch.bool)
        num_correct_drafts = candidates.new_zeros((batch_size,), dtype=torch.long)

    accept_index.fill_(-1)
    accept_index[:, 0] = retrive_index[:, 0].to(torch.int32)
    accept_token_num.copy_(num_correct_drafts.to(accept_token_num.dtype))
    if num_drafts:
        accept_index[:, 1:] = torch.where(
            correct,
            retrive_index[:, 1:].to(torch.int32),
            torch.full_like(accept_index[:, 1:], -1),
        )
        # Predicts starts at zero and chain indices are unique. Fixed-shape
        # writes avoid the device synchronization required by boolean indexing.
        predict_indices = retrive_index[:, :-1].reshape(-1)
        predict_values = torch.where(correct, candidate_tokens, 0)
        predicts[predict_indices] = predict_values.to(predicts.dtype).reshape(-1)

    current_rows = num_correct_drafts.clamp_max(num_slots - 1)
    residual = target_probs[batch_indices, current_rows].to(torch.float32)
    if num_drafts:
        rejected = num_correct_drafts < num_drafts
        rejected_tokens = candidates[
            batch_indices, (current_rows + 1).clamp_max(num_slots - 1)
        ].to(torch.long)
        rejected_probs = proposal_probs[
            batch_indices, current_rows.clamp_max(num_drafts - 1)
        ]
        residual.scatter_add_(
            dim=-1,
            index=rejected_tokens.unsqueeze(-1),
            src=(-rejected_probs * rejected).to(residual.dtype).unsqueeze(-1),
        )
        residual.clamp_(min=0)

    residual_sum = residual.sum(dim=-1)
    cdf = residual.cumsum(dim=-1)
    hit = cdf > (
        uniform_samples_for_final_sampling.to(torch.float32).unsqueeze(-1)
        * residual_sum.unsqueeze(-1)
    )
    sampled_ids = hit.to(torch.int32).argmax(dim=-1)
    sampled_ids = torch.where(hit.any(dim=-1), sampled_ids, residual.argmax(dim=-1))
    predicts[retrive_index[batch_indices, current_rows]] = sampled_ids.to(
        predicts.dtype
    )
