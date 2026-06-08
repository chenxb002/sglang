from typing import TYPE_CHECKING, Optional

import torch
import torch_mlu_ops
from sglang.srt.constants import GPU_MEMORY_TYPE_KV_CACHE
from sglang.srt.mem_cache.memory_pool import (
    MHATokenToKVPool,
    MLATokenToKVPool,
    get_tensor_size_bytes,
)
from sglang.srt.mem_cache.utils import get_mla_kv_buffer_triton

if TYPE_CHECKING:
    from sglang.srt.layers.radix_attention import RadixAttention


class MLUMHATokenToKVPool(MHATokenToKVPool):

    def _create_buffers(self):
        with self.memory_saver_adapter.region(GPU_MEMORY_TYPE_KV_CACHE):
            # [head_num, size, head_dim] for each layer
            # The padded slot 0 is used for writing dummy outputs from padded tokens.
            # The layout of kv cache is changed to:
            # - [2, layer_num, head_num, page_size, head_dim]
            # Note: in vllm, the layout of kv cache is:
            # dict{layer_id: (2, head_num, page_size, head_dim)}
            # Continuous memory improves the efficiency of MLU transmission backend,
            # while other backends remain unchanged.
            self.kv_buffer = torch.zeros(
                (
                    2,
                    self.layer_num,
                    self.size // self.page_size + 1,
                    self.head_num,
                    self.page_size,
                    self.head_dim,
                ),
                dtype=self.store_dtype,
                device=self.device,
            )
            self.k_buffer = self.kv_buffer[0]
            self.v_buffer = self.kv_buffer[1]

    # for disagg
    def get_contiguous_buf_infos(self):
        # layer_num x [seq_len, head_num, head_dim]
        # layer_num x [page_num, head_num, page_size, head_dim]
        kv_data_ptrs = [
            self.get_key_buffer(i).data_ptr()
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ] + [
            self.get_value_buffer(i).data_ptr()
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ]
        kv_data_lens = [
            self.get_key_buffer(i).nbytes
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ] + [
            self.get_value_buffer(i).nbytes
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ]
        kv_item_lens = [
            self.get_key_buffer(i)[0].nbytes
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ] + [
            self.get_value_buffer(i)[0].nbytes
            for i in range(self.start_layer, self.start_layer + self.layer_num)
        ]
        return kv_data_ptrs, kv_data_lens, kv_item_lens

    def set_kv_buffer(
        self,
        layer: "RadixAttention",
        loc: torch.Tensor,
        cache_k: torch.Tensor,
        cache_v: torch.Tensor,
        k_scale: Optional[float] = None,
        v_scale: Optional[float] = None,
        layer_id_override: Optional[int] = None,
    ):
        if layer_id_override is not None:
            layer_id = layer_id_override
        else:
            layer_id = layer.layer_id

        if self.store_dtype != self.dtype:
            cache_k = cache_k.to(self.store_dtype)
            cache_v = cache_v.to(self.store_dtype)

        # kv cache shape: (block_num, head_num, block_size, head_size)
        torch_mlu_ops.reshape_paged_cache(
            k=cache_k,
            v=cache_v,
            k_cache=self.k_buffer[layer_id - self.start_layer].view(
                -1, self.head_num, self.page_size, self.head_dim
            ),
            v_cache=self.v_buffer[layer_id - self.start_layer].view(
                -1, self.head_num, self.page_size, self.head_dim
            ),
            slot_mapping=loc.to(torch.int32),
        )


class MLUMLATokenToKVPool(MLATokenToKVPool):
    def __init__(
        self,
        size: int,
        page_size: int,
        dtype: torch.dtype,
        kv_lora_rank: int,
        qk_rope_head_dim: int,
        index_head_dim: Optional[int],
        layer_num: int,
        device: str,
        enable_memory_saver: bool,
        start_layer: Optional[int] = None,
        end_layer: Optional[int] = None,
    ):
        super(MLATokenToKVPool, self).__init__(
            size=size,
            page_size=page_size,
            dtype=dtype,
            layer_num=layer_num,
            device=device,
            enable_memory_saver=enable_memory_saver,
            start_layer=start_layer,
            end_layer=end_layer,
        )

        self.kv_lora_rank = kv_lora_rank
        self.qk_rope_head_dim = qk_rope_head_dim
        self.kv_cache_dim = kv_lora_rank + qk_rope_head_dim
        self.index_head_dim = index_head_dim

        self.custom_mem_pool = None

        with self.memory_saver_adapter.region(GPU_MEMORY_TYPE_KV_CACHE):
            # The padded slot 0 is used for writing dummy outputs from padded tokens.
            self.kv_buffer = torch.zeros(
                (
                    layer_num,
                    self.size // self.page_size + 1,
                    1,
                    self.page_size,
                    self.kv_cache_dim,
                ),
                dtype=self.store_dtype,
                device=self.device,
            )
            self.index_k_buffer = None
            if self.index_head_dim is not None:
                self.index_k_buffer = torch.zeros(
                    (
                        layer_num,
                        self.size // self.page_size + 1,
                        1,
                        self.page_size,
                        self.index_head_dim,
                    ),
                    dtype=self.store_dtype,
                    device=self.device,
                )

        self.data_ptrs = torch.tensor(
            [self.kv_buffer[i].data_ptr() for i in range(self.layer_num)],
            dtype=torch.uint64,
            device=self.device,
        )
        self._finalize_allocation_log(size)

    def get_kv_size_bytes(self):
        assert hasattr(self, "kv_buffer")
        kv_size_bytes = 0
        kv_size_bytes += get_tensor_size_bytes(self.kv_buffer)
        if self.index_head_dim is not None:
            assert hasattr(self, "index_k_buffer")
            for index_k_cache in self.index_k_buffer:
                kv_size_bytes += get_tensor_size_bytes(index_k_cache)
        return kv_size_bytes

    def get_index_k_buffer(self, layer_id: int):
        if self.layer_transfer_counter is not None:
            self.layer_transfer_counter.wait_until(layer_id - self.start_layer)

        if self.store_dtype != self.dtype:
            return self.index_k_buffer[layer_id - self.start_layer].view(self.dtype)
        return self.index_k_buffer[layer_id - self.start_layer]

    # for disagg
    def get_contiguous_buf_infos(self):
        # MLA has only one kv_buffer, so only the information of this buffer needs to be returned.
        kv_data_ptrs = [self.kv_buffer[i].data_ptr() for i in range(self.layer_num)]
        kv_data_lens = [self.kv_buffer[i].nbytes for i in range(self.layer_num)]
        kv_item_lens = [self.kv_buffer[i][0].nbytes for i in range(self.layer_num)]
        if self.index_head_dim is not None:
            kv_data_ptrs += [
                self.index_k_buffer[i].data_ptr() for i in range(self.layer_num)
            ]
            kv_data_lens += [
                self.index_k_buffer[i].nbytes for i in range(self.layer_num)
            ]
            kv_item_lens += [
                self.index_k_buffer[i][0].nbytes for i in range(self.layer_num)
            ]
        return kv_data_ptrs, kv_data_lens, kv_item_lens

    def set_kv_buffer(
        self,
        layer: "RadixAttention",
        loc: torch.Tensor,
        cache_k: torch.Tensor,
        cache_v: torch.Tensor,
    ):
        layer_id = layer.layer_id
        if cache_k.dtype != self.dtype:
            cache_k = cache_k.to(self.dtype)

        if self.store_dtype != self.dtype:
            cache_k = cache_k.view(self.store_dtype)

        torch_mlu_ops.reshape_paged_cache(
            k=cache_k,
            v=None,
            k_cache=self.kv_buffer[layer_id - self.start_layer].view(
                -1, 1, self.page_size, self.kv_cache_dim
            ),
            v_cache=None,
            slot_mapping=loc.to(torch.int32),
        )

    def get_mla_kv_buffer(
        self,
        layer: "RadixAttention",
        loc: torch.Tensor,
        dst_dtype: Optional[torch.dtype] = None,
    ):
        layer_id = layer.layer_id
        kv_buffer = self.get_key_buffer(layer_id)
        if not (
            kv_buffer.dim() == 4
            and kv_buffer.shape[1] == 1
            and kv_buffer.shape[2] == self.page_size
            and kv_buffer.shape[-1] == self.kv_cache_dim
        ):
            raise RuntimeError(
                f"Unsupported MLU MLA KV cache shape: {tuple(kv_buffer.shape)}"
            )
        if not kv_buffer.is_contiguous():
            raise RuntimeError(
                "MLU MLA KV cache must be contiguous before flattening; "
                f"shape={tuple(kv_buffer.shape)}, stride={kv_buffer.stride()}."
            )

        dst_dtype = dst_dtype or self.dtype
        flat_loc = loc.reshape(-1).to(device=kv_buffer.device, dtype=torch.int32)
        cache_k_nope = torch.empty(
            (flat_loc.shape[0], 1, self.kv_lora_rank),
            dtype=dst_dtype,
            device=kv_buffer.device,
        )
        cache_k_rope = torch.empty(
            (flat_loc.shape[0], 1, self.qk_rope_head_dim),
            dtype=dst_dtype,
            device=kv_buffer.device,
        )

        # MLU paged MLA layout before the view:
        #   [num_pages, 1, page_size, kv_cache_dim]
        # where physical token slot = page_id * page_size + page_offset.
        #
        # The community Triton gather kernel expects linear slot layout:
        #   [num_slots, 1, kv_cache_dim]
        # and indexes the first dimension directly with loc. The view below is a
        # zero-copy flatten of the contiguous page/page_offset dimensions.
        kv_buffer_linear = kv_buffer.view(-1, 1, self.kv_cache_dim)
        get_mla_kv_buffer_triton(
            kv_buffer_linear,
            flat_loc,
            cache_k_nope,
            cache_k_rope,
        )
        return cache_k_nope, cache_k_rope

    def set_index_k_buffer(
        self,
        layer_id: int,
        loc: torch.Tensor,
        index_k: torch.Tensor,
    ):
        if index_k.dtype != self.dtype:
            index_k = index_k.to(self.dtype)

        if self.store_dtype != self.dtype:
            index_k = index_k.view(self.store_dtype)

        torch_mlu_ops.reshape_paged_cache(
            k=index_k,
            v=None,
            k_cache=self.index_k_buffer[layer_id - self.start_layer].view(
                -1, 1, self.page_size, self.index_head_dim
            ),
            v_cache=None,
            slot_mapping=loc.to(torch.int32),
        )
