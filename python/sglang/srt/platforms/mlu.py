"""MLU device operations for the SRT platform layer."""

import logging
from typing import Optional

import torch

from sglang.srt.platforms.device_mixin import DeviceMixin, PlatformEnum
from sglang.srt.platforms.interface import SRTPlatform

logger = logging.getLogger(__name__)


class MluDeviceMixin(DeviceMixin):
    """Cambricon MLU implementation of shared device operations."""

    _enum: PlatformEnum = PlatformEnum.MLU
    device_name: str = "mlu"
    device_type: str = "mlu"

    def get_device_total_memory(self, device_id: int = 0) -> int:
        return int(torch.mlu.get_device_properties(device_id).total_memory)

    def get_current_memory_usage(
        self, device: Optional["torch.device"] = None
    ) -> float:
        torch.mlu.reset_peak_memory_stats(device)
        return float(torch.mlu.max_memory_allocated(device))

    def get_device(self, local_rank: int) -> "torch.device":
        return torch.device("mlu", local_rank)

    def set_device(self, device: "torch.device") -> None:
        torch.mlu.set_device(device)

    def get_device_name(self, device_id: int = 0) -> str:
        return str(torch.mlu.get_device_name(device_id))

    def get_device_uuid(self, device_id: int = 0) -> str:
        props = torch.mlu.get_device_properties(device_id)
        return str(getattr(props, "uuid", f"mlu:{device_id}"))

    def get_device_capability(self, device_id: int = 0):
        return None

    def empty_cache(self) -> None:
        torch.mlu.empty_cache()

    def synchronize(self) -> None:
        torch.mlu.synchronize()

    def get_available_memory(self, device_id: int = 0) -> tuple[int, int]:
        return torch.mlu.mem_get_info(device_id)

    def get_torch_distributed_backend_str(self) -> str:
        return "cncl"


class MluSRTPlatform(MluDeviceMixin, SRTPlatform):
    """Default in-tree Cambricon MLU SRT platform."""

    def apply_server_args_defaults(self, server_args) -> None:
        server_args.attention_backend = "cambricon"
        server_args.prefill_attention_backend = "cambricon"
        server_args.decode_attention_backend = "cambricon"
        if server_args.page_size is None:
            server_args.page_size = 16

        server_args.disable_custom_all_reduce = True

        if server_args.enable_hierarchical_cache:
            logger.warning("MLU does not support hierarchical cache; disabling it.")
            server_args.enable_hierarchical_cache = False

    def init_backend(self) -> None:
        from sglang.srt.hardware_backend.mlu import init_mlu_backend

        init_mlu_backend()

    def get_dispatch_key_name(self) -> str:
        return "mlu"

    def get_default_attention_backend(self) -> str:
        return "cambricon"

    def get_mha_kv_pool_cls(self) -> type:
        from sglang.srt.hardware_backend.mlu.memory_pool import MLUMHATokenToKVPool

        return MLUMHATokenToKVPool

    def get_mla_kv_pool_cls(self) -> type:
        raise NotImplementedError(
            "MLA KV pool is not included in the in-tree MLU demo/POC scope. "
            "Use an MHA model path for this prototype."
        )

    def get_paged_allocator_cls(self) -> type:
        from sglang.srt.mem_cache.allocator import PagedTokenToKVPoolAllocator

        return PagedTokenToKVPoolAllocator

    def support_cuda_graph(self) -> bool:
        return True

    def get_graph_runner_cls(self) -> type:
        from sglang.srt.hardware_backend.mlu.graph_runner import MLUGraphRunner

        return MLUGraphRunner

    def get_piecewise_backend_cls(self) -> type:
        raise NotImplementedError(
            "The MLU POC does not support piecewise graph compilation yet. "
            "Keep disable_piecewise_cuda_graph=True."
        )
