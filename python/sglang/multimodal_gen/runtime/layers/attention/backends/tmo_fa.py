# TMO Flash Attention Backend for MLU Platform

import torch
from diffusion_ops import tmo_flash_attention

from sglang.multimodal_gen.runtime.layers.attention.backends.attention_backend import (
    AttentionBackend,
    AttentionImpl,
    AttentionMetadata,
    AttentionMetadataBuilder,
)
from sglang.multimodal_gen.runtime.platforms import AttentionBackendEnum
from sglang.multimodal_gen.runtime.utils.logging_utils import init_logger

logger = init_logger(__name__)


class TmoFABackend(AttentionBackend):
    """TMO Flash Attention Backend for MLU platform."""

    accept_output_buffer: bool = True

    @staticmethod
    def get_enum() -> AttentionBackendEnum:
        return AttentionBackendEnum.TMO_FA

    @staticmethod
    def get_impl_cls() -> type["TmoFAImpl"]:
        return TmoFAImpl

    @staticmethod
    def get_metadata_cls() -> type["AttentionMetadata"]:
        raise NotImplementedError

    @staticmethod
    def get_builder_cls() -> type["AttentionMetadataBuilder"]:
        raise NotImplementedError


class TmoFAImpl(AttentionImpl):
    """TMO Flash Attention Implementation."""

    def __init__(
        self,
        num_heads: int,
        head_size: int,
        causal: bool,
        softmax_scale: float,
        num_kv_heads: int | None = None,
        prefix: str = "",
        **extra_impl_args,
    ) -> None:
        # Only store parameters used in forward
        self.causal = causal
        self.softmax_scale = softmax_scale

    def forward(
        self,
        query: torch.Tensor,  # [B, S, H, D]
        key: torch.Tensor,
        value: torch.Tensor,
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        """
        Forward pass for TMO Flash Attention.

        Args:
            query: [B, S, H, D]
            key: [B, S, H, D]
            value: [B, S, H, D]

        Returns:
            output: [B, S, H, D]
        """
        H = query.size(2)
        D = value.size(3)

        out = tmo_flash_attention(
            q=query,
            k=key,
            v=value,
            softmax_scale=self.softmax_scale,
            is_causal=self.causal,
            input_layout="nthc",
        )

        out = out.unflatten(2, (H, D))
        return out
