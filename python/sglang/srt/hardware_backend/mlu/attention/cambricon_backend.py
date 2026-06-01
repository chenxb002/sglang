from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import torch
import torch_mlu_ops

from sglang.srt.layers.attention.base_attn_backend import AttentionBackend
from sglang.srt.model_executor.forward_batch_info import ForwardBatch, ForwardMode
from sglang.srt.speculative.spec_info import SpecInput

if TYPE_CHECKING:
    from sglang.srt.layers.radix_attention import RadixAttention
    from sglang.srt.model_executor.model_runner import ModelRunner

logger = logging.getLogger(__name__)


def _raise_if_mla_inputs(
    layer: "RadixAttention",
    q_rope: Optional[torch.Tensor],
    k_rope: Optional[torch.Tensor],
    topk_indices: Optional[torch.Tensor],
) -> None:
    if q_rope is not None or k_rope is not None or topk_indices is not None:
        raise NotImplementedError(
            "MLA attention is not included in the in-tree MLU demo/POC scope."
        )
    if layer.qk_head_dim != layer.v_head_dim:
        raise NotImplementedError(
            "MLA attention is not included in the in-tree MLU demo/POC scope."
        )


@dataclass
class ForwardMetadata:
    """Metadata for attention forward pass."""

    # Attention tensors (required for EXTEND/DECODE, optional for MIXED)
    cu_seqlens_q: Optional[torch.Tensor] = None
    cu_seqlens_kv: Optional[torch.Tensor] = None
    max_seq_len_q: int = 0
    max_seq_len_kv: int = 0

    # KV cache indexing (common to all modes)
    block_tables: Optional[torch.Tensor] = None
    seq_lens: Optional[torch.Tensor] = None

    # MIXED mode only: indices separating prefill and decode requests
    prefill_indices: Optional[tuple[int, ...]] = None
    decode_indices: Optional[tuple[int, ...]] = None

    # EXTEND mode only: whether this is a pure prefill without cached prefix
    is_uncached_prefill_only: bool = True

    # Attention compute dtype
    compute_dtype: torch.dtype = torch.float32


class CambriconAttnBackend(AttentionBackend):

    def __init__(self, model_runner: ModelRunner):
        super().__init__()
        self.forward_metadata = None
        self.device = model_runner.device
        self.page_size = model_runner.page_size
        self.max_context_len = model_runner.model_config.context_len
        self.req_to_token = model_runner.req_to_token_pool.req_to_token

    def init_forward_metadata(self, forward_batch: ForwardBatch):
        """Init metadata based on forward mode."""
        self.forward_metadata = ForwardMetadata()
        meta = self.forward_metadata
        mode = forward_batch.forward_mode

        # Common: block_tables and seq_lens for all modes
        meta.block_tables = (
            forward_batch.req_to_token_pool.req_to_token[
                forward_batch.req_pool_indices, : self.max_context_len
            ][:, :: self.page_size]
            // self.page_size
        )
        meta.seq_lens = forward_batch.seq_lens.to(dtype=torch.int32)

        if mode == ForwardMode.EXTEND:
            batch_size = forward_batch.batch_size

            # Compute cu_seqlens_q from extend_seq_lens
            meta.cu_seqlens_q = torch.zeros(
                batch_size + 1, dtype=torch.int32, device=self.device
            )
            torch.cumsum(
                forward_batch.extend_seq_lens,
                dim=0,
                out=meta.cu_seqlens_q[1:],
            )
            meta.max_seq_len_q = max(forward_batch.extend_seq_lens_cpu)

            # Check if all requests have no cached prefix
            meta.is_uncached_prefill_only = (
                forward_batch.extend_prefix_lens.sum().item() == 0
            )

            if meta.is_uncached_prefill_only:
                # No prefix: q and kv have same sequence lengths
                meta.cu_seqlens_kv = meta.cu_seqlens_q.clone()
                meta.max_seq_len_kv = meta.max_seq_len_q
            else:
                # Has prefix: kv must cover prefix + extend tokens
                seq_lens = forward_batch.seq_lens.to(dtype=torch.int32)
                meta.cu_seqlens_kv = torch.zeros(
                    batch_size + 1, dtype=torch.int32, device=self.device
                )
                torch.cumsum(seq_lens, dim=0, out=meta.cu_seqlens_kv[1:])
                meta.max_seq_len_kv = int(seq_lens.max().item())

        elif mode == ForwardMode.DECODE:
            batch_size = forward_batch.batch_size
            # Simplified: use arange instead of zeros + cumsum
            meta.cu_seqlens_q = torch.arange(
                batch_size + 1, dtype=torch.int32, device=self.device
            )
            meta.cu_seqlens_kv = torch.zeros(
                batch_size + 1, dtype=torch.int32, device=self.device
            )
            meta.cu_seqlens_kv[1:] = forward_batch.seq_lens.to(dtype=torch.int32)
            torch.cumsum(meta.cu_seqlens_kv, dim=0, out=meta.cu_seqlens_kv)
            meta.max_seq_len_q = 1
            meta.max_seq_len_kv = int(forward_batch.seq_lens.max().item())

        elif mode == ForwardMode.MIXED:
            batch_size = forward_batch.batch_size
            running_bs = (
                0
                if forward_batch.mix_running_indices is None
                else len(forward_batch.mix_running_indices)
            )
            prefill_bs = batch_size - running_bs
            assert prefill_bs >= 0, (
                f"Invalid mixed batch boundary: batch_size={batch_size}, "
                f"running_bs={running_bs}."
            )

            # In MIXED mode, ScheduleBatch.mix_with_running appends the running
            # decode batch after the prefill requests. A prefill request may also
            # have a one-token extend chunk, so do not infer the boundary from
            # extend_seq_lens.
            meta.prefill_indices = tuple(range(prefill_bs))
            meta.decode_indices = tuple(range(prefill_bs, batch_size))

    def init_cuda_graph_state(self, max_bs: int, max_num_tokens: int):
        self.graph_metadata = {
            "block_tables": torch.empty(
                (max_bs, (self.max_context_len + self.page_size - 1) // self.page_size),
                dtype=torch.int32,
                device=self.device,
            ),
        }

    def init_forward_metadata_capture_cuda_graph(
        self,
        bs: int,
        num_tokens: int,
        req_pool_indices: torch.Tensor,
        seq_lens: torch.Tensor,
        encoder_lens: Optional[torch.Tensor],
        forward_mode: ForwardMode,
        spec_info: Optional[SpecInput],
    ):
        metadata = ForwardMetadata()
        metadata.block_tables = self.graph_metadata["block_tables"][:bs, :]
        metadata.seq_lens = seq_lens
        metadata.cu_seqlens_q = torch.arange(
            bs + 1, dtype=torch.int32, device=seq_lens.device
        )
        self.graph_metadata[bs] = metadata
        self.forward_metadata = metadata
        self.graph_mode = True

    def get_cuda_graph_seq_len_fill_value(self):
        return 0

    def init_forward_metadata_replay_cuda_graph(
        self,
        bs: int,
        req_pool_indices: torch.Tensor,
        seq_lens: torch.Tensor,
        seq_lens_sum: int,
        encoder_lens: Optional[torch.Tensor],
        forward_mode: ForwardMode,
        spec_info: Optional[SpecInput],
        seq_lens_cpu: Optional[torch.Tensor],
    ):
        metadata = self.graph_metadata[bs]
        max_len = seq_lens_cpu[:bs].max().item()
        if forward_mode.is_target_verify():
            max_len += self.speculative_num_draft_tokens
        max_seq_pages = (max_len + self.page_size - 1) // self.page_size

        metadata.block_tables[:bs, :max_seq_pages].copy_(
            self.req_to_token[req_pool_indices[:bs], :max_len][:, :: self.page_size]
            // self.page_size
        )
        metadata.block_tables[bs:, :].fill_(0)
        if forward_mode.is_target_verify():
            seq_lens = seq_lens + self.speculative_num_draft_tokens
        metadata.seq_lens[:bs].copy_(seq_lens[:bs])

        self.forward_metadata = metadata
        self.graph_mode = True

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        layer: RadixAttention,
        forward_batch: ForwardBatch,
        save_kv_cache: bool = True,
        **kwargs,
    ):
        """Entry point - dispatch to appropriate forward method based on mode."""
        mode = forward_batch.forward_mode
        if mode.is_idle():
            return q.new_empty(q.shape[0], layer.tp_q_head_num * layer.v_head_dim)
        elif mode.is_decode():
            return self.forward_decode(
                q, k, v, layer, forward_batch, save_kv_cache, **kwargs
            )
        elif mode.is_mixed():
            return self.forward_mixed(
                q, k, v, layer, forward_batch, save_kv_cache, **kwargs
            )
        else:
            return self.forward_extend(
                q, k, v, layer, forward_batch, save_kv_cache, **kwargs
            )

    def forward_extend(
        self,
        q,
        k,
        v,
        layer: RadixAttention,
        forward_batch: ForwardBatch,
        save_kv_cache: bool = True,
        q_rope: Optional[torch.Tensor] = None,
        k_rope: Optional[torch.Tensor] = None,
        topk_indices: Optional[torch.Tensor] = None,
    ):
        """Pure prefill/extend mode - handles first token generation for new requests."""
        _raise_if_mla_inputs(layer, q_rope, k_rope, topk_indices)
        meta = self.forward_metadata

        # Reshape tensors
        q = q.view(-1, layer.tp_q_head_num, layer.qk_head_dim)
        k = k.view(-1, layer.tp_k_head_num, layer.qk_head_dim)
        v = v.view(-1, layer.tp_v_head_num, layer.v_head_dim)

        # Write to KV cache before attention
        if save_kv_cache:
            forward_batch.token_to_kv_pool.set_kv_buffer(
                layer, forward_batch.out_cache_loc, k, v
            )

        if meta.is_uncached_prefill_only:
            # Direct attention without prefix KV cache
            out = torch_mlu_ops.flash_attention(
                q=q,
                k=k,
                v=v,
                out=None,
                cu_seq_lens_q=meta.cu_seqlens_q,
                cu_seq_lens_kv=meta.cu_seqlens_kv,
                alibi_slope=None,
                attn_bias=None,
                max_seq_len_q=meta.max_seq_len_q,
                max_seq_len_kv=meta.max_seq_len_kv,
                softmax_scale=layer.scaling,
                is_causal=True,
                compute_dtype=meta.compute_dtype,
                return_lse=False,
                block_tables=None,
            )
        else:
            k_cache = forward_batch.token_to_kv_pool.get_key_buffer(layer.layer_id)
            v_cache = forward_batch.token_to_kv_pool.get_value_buffer(layer.layer_id)

            out = torch_mlu_ops.flash_attention(
                q=q,
                k=k_cache,
                v=v_cache,
                out=None,
                cu_seq_lens_q=meta.cu_seqlens_q,
                cu_seq_lens_kv=meta.cu_seqlens_kv,
                alibi_slope=None,
                attn_bias=None,
                max_seq_len_q=meta.max_seq_len_q,
                max_seq_len_kv=meta.max_seq_len_kv,
                softmax_scale=layer.scaling,
                is_causal=True,
                compute_dtype=meta.compute_dtype,
                return_lse=False,
                block_tables=meta.block_tables,
                out_dtype=torch.bfloat16,
            )

        return out.view(-1, layer.tp_q_head_num * layer.v_head_dim)

    def forward_decode(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        layer: RadixAttention,
        forward_batch: ForwardBatch,
        save_kv_cache: bool = True,
        q_rope: Optional[torch.Tensor] = None,
        k_rope: Optional[torch.Tensor] = None,
        topk_indices: Optional[torch.Tensor] = None,
    ):
        """Pure decode mode - handles continuation token generation."""
        _raise_if_mla_inputs(layer, q_rope, k_rope, topk_indices)
        batch_size = forward_batch.batch_size
        meta = self.forward_metadata

        # Reshape tensors
        k = k.view(-1, layer.tp_k_head_num, layer.qk_head_dim)
        v = v.view(-1, layer.tp_v_head_num, layer.v_head_dim)
        q = q.view(batch_size, -1, layer.tp_q_head_num, layer.qk_head_dim)

        if save_kv_cache:
            forward_batch.token_to_kv_pool.set_kv_buffer(
                layer, forward_batch.out_cache_loc, k, v
            )

        k_cache = forward_batch.token_to_kv_pool.get_key_buffer(layer.layer_id)
        v_cache = forward_batch.token_to_kv_pool.get_value_buffer(layer.layer_id)

        out = torch_mlu_ops.single_query_cached_kv_attn(
            q=q,
            k_cache=k_cache,
            v_cache=v_cache,
            out=None,
            block_tables=self.forward_metadata.block_tables,
            context_lens=self.forward_metadata.seq_lens,
            k_cache_quant_scale=None,
            v_cache_quant_scale=None,
            alibi_slopes=None,
            max_contxt_len=self.max_context_len,
            windows_size_left=-1,
            windows_size_right=-1,
            softmax_scale=layer.scaling,
            head_size_v=-1,
            compute_dtype=meta.compute_dtype,
            q_quant_scale=None,
            out_quant_scale=None,
        )

        return out.view(batch_size, -1)

    def forward_mixed(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        layer: RadixAttention,
        forward_batch: ForwardBatch,
        save_kv_cache: bool = True,
        q_rope: Optional[torch.Tensor] = None,
        k_rope: Optional[torch.Tensor] = None,
        topk_indices: Optional[torch.Tensor] = None,
    ):
        """MIXED mode - chunked prefill + decode mixed.

        q tensor layout: [prefill_tokens, decode_tokens]
        """
        _raise_if_mla_inputs(layer, q_rope, k_rope, topk_indices)
        meta = self.forward_metadata
        prefill_idx, decode_idx = meta.prefill_indices, meta.decode_indices

        # Reshape k/v before writing to KV cache
        k = k.view(-1, layer.tp_k_head_num, layer.qk_head_dim)
        v = v.view(-1, layer.tp_v_head_num, layer.v_head_dim)

        if save_kv_cache:
            forward_batch.token_to_kv_pool.set_kv_buffer(
                layer, forward_batch.out_cache_loc, k, v
            )

        k_cache = forward_batch.token_to_kv_pool.get_key_buffer(layer.layer_id)
        v_cache = forward_batch.token_to_kv_pool.get_value_buffer(layer.layer_id)

        output = q.new_empty((q.shape[0], layer.tp_q_head_num * layer.v_head_dim))

        num_prefill = 0

        # Process prefill chunk (prefill indices are always first)
        if prefill_idx:
            prefill_lens = forward_batch.extend_seq_lens[list(prefill_idx)]
            prefill_seq_lens = meta.seq_lens[list(prefill_idx)]
            num_prefill = int(prefill_lens.sum().item())

            chunk_q = q[:num_prefill].view(-1, layer.tp_q_head_num, layer.qk_head_dim)

            chunk_output = output[:num_prefill].view(
                -1, layer.tp_q_head_num, layer.v_head_dim
            )

            # Build cu_seqlens for this chunk
            cu_q = torch.zeros(
                len(prefill_idx) + 1, dtype=torch.int32, device=self.device
            )
            cu_q[1:] = prefill_lens.to(dtype=torch.int32)
            torch.cumsum(cu_q, dim=0, out=cu_q)

            cu_kv = torch.zeros(
                len(prefill_idx) + 1, dtype=torch.int32, device=self.device
            )
            cu_kv[1:] = prefill_seq_lens.to(dtype=torch.int32)
            torch.cumsum(cu_kv, dim=0, out=cu_kv)

            # Execute flash attention - write directly to chunk_output
            torch_mlu_ops.flash_attention(
                q=chunk_q,
                k=k_cache,
                v=v_cache,
                out=chunk_output,
                cu_seq_lens_q=cu_q,
                cu_seq_lens_kv=cu_kv,
                alibi_slope=None,
                attn_bias=None,
                max_seq_len_q=int(prefill_lens.max().item()),
                max_seq_len_kv=int(prefill_seq_lens.max().item()),
                softmax_scale=layer.scaling,
                is_causal=True,
                compute_dtype=meta.compute_dtype,
                return_lse=False,
                block_tables=meta.block_tables[list(prefill_idx)],
                out_dtype=torch.bfloat16,
            )

        # Process decode chunk (decode indices are always after prefill)
        if decode_idx:
            num_decode = len(decode_idx)
            chunk_q = q[num_prefill : num_prefill + num_decode]
            chunk_output = output[num_prefill : num_prefill + num_decode]
            decode_seq_lens = meta.seq_lens[list(decode_idx)]

            chunk_q = chunk_q.view(
                num_decode, -1, layer.tp_q_head_num, layer.qk_head_dim
            )
            chunk_output = chunk_output.view(
                num_decode, -1, layer.tp_q_head_num, layer.v_head_dim
            )

            torch_mlu_ops.single_query_cached_kv_attn(
                q=chunk_q,
                k_cache=k_cache,
                v_cache=v_cache,
                out=chunk_output,
                block_tables=meta.block_tables[list(decode_idx)],
                context_lens=decode_seq_lens,
                k_cache_quant_scale=None,
                v_cache_quant_scale=None,
                alibi_slopes=None,
                max_contxt_len=int(decode_seq_lens.max().item()),
                windows_size_left=-1,
                windows_size_right=-1,
                softmax_scale=layer.scaling,
                head_size_v=-1,
                compute_dtype=meta.compute_dtype,
                q_quant_scale=None,
                out_quant_scale=None,
            )

        return output
