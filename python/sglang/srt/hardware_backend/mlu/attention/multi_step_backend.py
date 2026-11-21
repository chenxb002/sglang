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
"""MLU attention backend wrapper for multi-step draft decoding."""

from copy import copy

from sglang.srt.layers.attention.attention_registry import create_mlu_backend
from sglang.srt.speculative.eagle_utils import per_step_draft_out_cache_loc


class MLUMultiStepDraftBackend:
    """Wrap several MLU decode backends for topk=1 chain MTP decoding."""

    def __init__(self, model_runner, topk: int, speculative_num_steps: int):
        if topk != 1:
            raise NotImplementedError(
                "MLU multi-step draft backend supports topk=1 only."
            )
        if speculative_num_steps <= 1:
            raise ValueError("MLU multi-step draft backend requires steps > 1.")

        self.topk = topk
        self.speculative_num_steps = speculative_num_steps
        self.attn_backends = [
            create_mlu_backend(model_runner) for _ in range(speculative_num_steps - 1)
        ]

    def init_forward_metadata(self, forward_batch):
        # Step i attends to the prefix plus i + 1 already materialized draft tokens.
        original_seq_lens = forward_batch.seq_lens
        original_seq_lens_cpu = forward_batch.seq_lens_cpu[: forward_batch.batch_size]
        original_seq_lens_sum = forward_batch.seq_lens_sum
        locations = per_step_draft_out_cache_loc(
            forward_batch.out_cache_loc,
            forward_batch.batch_size,
            self.topk,
            self.speculative_num_steps,
        )
        for i, attn_backend in enumerate(self.attn_backends):
            seq_len_offset = i + 1
            step_batch = copy(forward_batch)
            step_batch.out_cache_loc = locations[i]
            step_batch.seq_lens = original_seq_lens + seq_len_offset
            step_batch.seq_lens_cpu = original_seq_lens_cpu + seq_len_offset
            step_batch.seq_lens_sum = (
                original_seq_lens_sum + forward_batch.batch_size * seq_len_offset
            )
            attn_backend.init_forward_metadata(step_batch)
