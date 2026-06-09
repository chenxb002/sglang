import importlib
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import torch

from sglang.srt.model_executor.forward_batch_info import ForwardMode
from sglang.test.ci.ci_register import register_cpu_ci
from sglang.test.test_utils import CustomTestCase

register_cpu_ci(est_time=12, suite="base-a-test-cpu")


def _fake_mlu_ops():
    return SimpleNamespace(
        reshape_paged_cache=MagicMock(),
        flash_attention=MagicMock(),
        single_query_cached_kv_attn=MagicMock(),
    )


class TestMLUPyproject(CustomTestCase):
    def test_mlu_pyproject_excludes_cuda_only_dependencies(self):
        pyproject = Path("python/pyproject_mlu.toml").read_text()
        cuda_only = [
            "cuda-python",
            "flashinfer_python",
            "flashinfer_cubin",
            "nvidia-cutlass-dsl",
            "flash-attn-4",
            "sgl-deep-gemm",
            "sglang-kernel",
            "tilelang",
            "tokenspeed_mla",
            "torch==",
            "torchaudio==",
            "torchvision",
        ]
        for dependency in cuda_only:
            with self.subTest(dependency=dependency):
                self.assertNotIn(dependency, pyproject)

        self.assertIn("srt_mlu", pyproject)
        self.assertIn("all_mlu", pyproject)
        self.assertIn("dev_mlu", pyproject)


class TestMLUKVCacheUnits(CustomTestCase):
    def test_mha_pool_uses_mlu_paged_cache_writer(self):
        fake_ops = _fake_mlu_ops()
        with patch.dict(sys.modules, {"torch_mlu_ops": fake_ops}):
            memory_pool_mod = importlib.import_module(
                "sglang.srt.hardware_backend.mlu.memory_pool"
            )
        with patch.object(memory_pool_mod, "torch_mlu_ops", fake_ops):
            MLUMHATokenToKVPool = memory_pool_mod.MLUMHATokenToKVPool

            pool = MLUMHATokenToKVPool(
                size=32,
                page_size=16,
                dtype=torch.float32,
                head_num=2,
                head_dim=4,
                layer_num=2,
                device="cpu",
                enable_memory_saver=False,
                enable_alt_stream=False,
            )

            self.assertEqual(pool.kv_buffer.shape, (2, 2, 3, 2, 16, 4))
            self.assertTrue(pool.kv_buffer.is_contiguous())

            layer = SimpleNamespace(layer_id=1)
            loc = torch.tensor([0, 17], dtype=torch.int64)
            cache_k = torch.randn(2, 2, 4)
            cache_v = torch.randn(2, 2, 4)
            pool.set_kv_buffer(layer, loc, cache_k, cache_v)

            fake_ops.reshape_paged_cache.assert_called_once()
            kwargs = fake_ops.reshape_paged_cache.call_args.kwargs
            self.assertEqual(kwargs["slot_mapping"].dtype, torch.int32)
            self.assertEqual(tuple(kwargs["k_cache"].shape), (3, 2, 16, 4))
            self.assertEqual(tuple(kwargs["v_cache"].shape), (3, 2, 16, 4))

    def test_mla_pool_layout_and_index_cache_writer(self):
        fake_ops = _fake_mlu_ops()
        with patch.dict(sys.modules, {"torch_mlu_ops": fake_ops}):
            memory_pool_mod = importlib.import_module(
                "sglang.srt.hardware_backend.mlu.memory_pool"
            )
        with patch.object(memory_pool_mod, "torch_mlu_ops", fake_ops):
            MLUMLATokenToKVPool = memory_pool_mod.MLUMLATokenToKVPool

            pool = MLUMLATokenToKVPool(
                size=32,
                page_size=16,
                dtype=torch.float32,
                kv_lora_rank=4,
                qk_rope_head_dim=2,
                index_head_dim=3,
                layer_num=2,
                device="cpu",
                enable_memory_saver=False,
            )

            self.assertEqual(pool.kv_buffer.shape, (2, 3, 1, 16, 6))
            self.assertEqual(pool.index_k_buffer.shape, (2, 3, 1, 16, 3))

            loc = torch.tensor([0, 17], dtype=torch.int64)
            layer = SimpleNamespace(layer_id=1)
            cache_k = torch.randn(2, 1, 6)
            pool.set_kv_buffer(layer, loc, cache_k, None)
            pool.set_index_k_buffer(layer_id=1, loc=loc, index_k=torch.randn(2, 1, 3))

            self.assertEqual(fake_ops.reshape_paged_cache.call_count, 2)
            first_call = fake_ops.reshape_paged_cache.call_args_list[0].kwargs
            self.assertEqual(tuple(first_call["k_cache"].shape), (3, 1, 16, 6))
            second_call = fake_ops.reshape_paged_cache.call_args_list[1].kwargs
            self.assertEqual(tuple(second_call["k_cache"].shape), (3, 1, 16, 3))

    def test_mla_flattened_gather_uses_linear_slot_layout(self):
        fake_ops = _fake_mlu_ops()
        with patch.dict(sys.modules, {"torch_mlu_ops": fake_ops}):
            memory_pool_mod = importlib.import_module(
                "sglang.srt.hardware_backend.mlu.memory_pool"
            )
        with patch.object(memory_pool_mod, "torch_mlu_ops", fake_ops):
            MLUMLATokenToKVPool = memory_pool_mod.MLUMLATokenToKVPool

            pool = MLUMLATokenToKVPool(
                size=32,
                page_size=16,
                dtype=torch.float32,
                kv_lora_rank=4,
                qk_rope_head_dim=2,
                index_head_dim=None,
                layer_num=1,
                device="cpu",
                enable_memory_saver=False,
            )
            flat_cache = pool.kv_buffer[0].view(-1, 1, pool.kv_cache_dim)
            flat_cache[0, 0] = torch.tensor([1, 2, 3, 4, 5, 6], dtype=torch.float32)
            flat_cache[17, 0] = torch.tensor(
                [11, 12, 13, 14, 15, 16], dtype=torch.float32
            )

            def fake_gather(kv_buffer, loc, cache_k_nope, cache_k_rope):
                selected = kv_buffer[loc.long()]
                cache_k_nope.copy_(selected[..., : pool.kv_lora_rank])
                cache_k_rope.copy_(selected[..., pool.kv_lora_rank :])

            with patch.object(
                memory_pool_mod,
                "get_mla_kv_buffer_triton",
                side_effect=fake_gather,
            ) as gather:
                nope, rope = pool.get_mla_kv_buffer(
                    SimpleNamespace(layer_id=0),
                    torch.tensor([0, 17], dtype=torch.int64),
                )

            gather.assert_called_once()
            kv_buffer_arg, loc_arg, _, _ = gather.call_args.args
            self.assertEqual(tuple(kv_buffer_arg.shape), (48, 1, 6))
            self.assertEqual(loc_arg.dtype, torch.int32)
            self.assertEqual(nope.squeeze(1).tolist(), [[1, 2, 3, 4], [11, 12, 13, 14]])
            self.assertEqual(rope.squeeze(1).tolist(), [[5, 6], [15, 16]])


class TestMLUPagedAllocator(CustomTestCase):
    def test_alloc_extend_uses_soft_i64_kernel_and_advances_pages(self):
        allocator_mod = importlib.import_module(
            "sglang.srt.hardware_backend.mlu.allocator"
        )

        class FakeKernel:
            def __init__(self):
                self.grid = None
                self.kwargs = None

            def __getitem__(self, grid):
                self.grid = grid

                def launch(*args, **kwargs):
                    self.kwargs = kwargs
                    out_indices = args[4]
                    out_indices.copy_(torch.arange(out_indices.numel()))

                return launch

        fake_kernel = FakeKernel()
        with (
            patch.object(allocator_mod, "alloc_extend_kernel", fake_kernel),
            patch.object(allocator_mod, "get_num_new_pages", return_value=2),
        ):
            allocator = allocator_mod.MLUPagedTokenToKVPoolAllocator(
                size=64,
                page_size=16,
                dtype=torch.float32,
                device="cpu",
                kvcache=MagicMock(),
                need_sort=False,
            )
            out = allocator.alloc_extend(
                prefix_lens=torch.tensor([0, 16], dtype=torch.int64),
                prefix_lens_cpu=torch.tensor([0, 16], dtype=torch.int64),
                seq_lens=torch.tensor([18, 20], dtype=torch.int64),
                seq_lens_cpu=torch.tensor([18, 20], dtype=torch.int64),
                last_loc=torch.tensor([-1, 15], dtype=torch.int64),
                extend_num_tokens=6,
            )

        self.assertEqual(fake_kernel.grid, (2,))
        self.assertEqual(fake_kernel.kwargs, {"enable_soft_i64": True})
        self.assertEqual(out.tolist(), [0, 1, 2, 3, 4, 5])
        self.assertEqual(allocator.free_pages.tolist(), [3, 4])


class TestMLUAttentionMetadata(CustomTestCase):
    def _make_backend(self):
        fake_ops = _fake_mlu_ops()
        with patch.dict(sys.modules, {"torch_mlu_ops": fake_ops}):
            attn_mod = importlib.import_module(
                "sglang.srt.hardware_backend.mlu.attention.mlu_backend"
            )
        with patch.object(attn_mod, "torch_mlu_ops", fake_ops):
            MLUAttnBackend = attn_mod.MLUAttnBackend

            runner = SimpleNamespace(
                device="cpu",
                page_size=16,
                model_config=SimpleNamespace(context_len=64),
                req_to_token_pool=SimpleNamespace(
                    req_to_token=torch.arange(2 * 64, dtype=torch.int32).reshape(2, 64)
                ),
                token_to_kv_pool=MagicMock(),
            )
            return MLUAttnBackend(runner)

    def test_extend_metadata_tracks_uncached_prefill(self):
        backend = self._make_backend()
        forward_batch = SimpleNamespace(
            forward_mode=ForwardMode.EXTEND,
            req_pool_indices=torch.tensor([0, 1], dtype=torch.int64),
            seq_lens=torch.tensor([4, 5], dtype=torch.int32),
            extend_seq_lens=torch.tensor([4, 5], dtype=torch.int32),
            extend_seq_lens_cpu=[4, 5],
            extend_prefix_lens=torch.tensor([0, 0], dtype=torch.int32),
            batch_size=2,
        )

        backend.init_forward_metadata(forward_batch)
        meta = backend.forward_metadata

        self.assertTrue(meta.is_uncached_prefill_only)
        self.assertEqual(meta.max_seq_len_q, 5)
        self.assertEqual(meta.max_seq_len_kv, 5)
        self.assertEqual(meta.cu_seqlens_q.tolist(), [0, 4, 9])
        self.assertEqual(meta.cu_seqlens_kv.tolist(), [0, 4, 9])
        self.assertEqual(tuple(meta.block_tables.shape), (2, 4))

    def test_decode_metadata_uses_single_token_queries(self):
        backend = self._make_backend()
        forward_batch = SimpleNamespace(
            forward_mode=ForwardMode.DECODE,
            req_pool_indices=torch.tensor([0, 1], dtype=torch.int64),
            seq_lens=torch.tensor([7, 9], dtype=torch.int32),
            batch_size=2,
        )

        backend.init_forward_metadata(forward_batch)
        meta = backend.forward_metadata

        self.assertEqual(meta.max_seq_len_q, 1)
        self.assertEqual(meta.max_seq_len_kv, 9)
        self.assertEqual(meta.cu_seqlens_q.tolist(), [0, 1, 2])
        self.assertEqual(meta.cu_seqlens_kv.tolist(), [0, 7, 16])


class TestMLUGraphRunner(CustomTestCase):
    def test_graph_runner_uses_mlu_graph_api(self):
        graph_runner_mod = importlib.import_module(
            "sglang.srt.hardware_backend.mlu.graph_runner"
        )

        fake_graph = object()
        fake_pool = object()
        fake_stream = object()
        fake_graph_ctx = MagicMock()
        fake_graph_ctx.__enter__.return_value = None
        fake_graph_ctx.__exit__.return_value = None
        fake_mlu = SimpleNamespace(
            MLUGraph=MagicMock(return_value=fake_graph),
            graph=MagicMock(return_value=fake_graph_ctx),
        )
        fake_torch = SimpleNamespace(mlu=fake_mlu, int32=torch.int32)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    graph_runner_mod.CudaGraphRunner, "__init__", return_value=None
                )
            )
            stack.enter_context(patch.object(graph_runner_mod, "torch", fake_torch))
            runner = graph_runner_mod.MLUGraphRunner(SimpleNamespace())
            created_graph = runner._create_device_graph()
            cache_loc_dtype = runner._cache_loc_dtype()
            out = runner._capture_graph(
                fake_graph, fake_pool, fake_stream, lambda: "captured"
            )

        self.assertEqual(runner.attr_name, {})
        self.assertEqual(runner.attr_type, {})
        self.assertEqual(cache_loc_dtype, torch.int32)
        self.assertIs(created_graph, fake_graph)
        fake_mlu.graph.assert_called_once_with(
            fake_graph, pool=fake_pool, stream=fake_stream
        )
        self.assertEqual(out, "captured")


class TestMLUCommunicator(CustomTestCase):
    def test_communicator_disabled_when_platform_is_not_mlu(self):
        comm_mod = importlib.import_module(
            "sglang.srt.distributed.device_communicators.mlu_communicator"
        )
        with patch.object(comm_mod.current_platform, "is_mlu", return_value=False):
            comm = comm_mod.MluCommunicator(group=MagicMock())
        self.assertTrue(comm.disabled)

    def test_all_reduce_and_all_gather_dispatch_to_torch_distributed(self):
        comm_mod = importlib.import_module(
            "sglang.srt.distributed.device_communicators.mlu_communicator"
        )
        group = MagicMock()

        def fake_all_gather_into_tensor(output, x, group):
            output[: x.shape[0]].copy_(x)
            output[x.shape[0] :].copy_(x + 10)

        with (
            patch.object(comm_mod.current_platform, "is_mlu", return_value=True),
            patch.object(comm_mod.dist, "get_world_size", return_value=2),
            patch.object(comm_mod.dist, "all_reduce") as all_reduce,
            patch.object(
                comm_mod.dist,
                "all_gather_into_tensor",
                side_effect=fake_all_gather_into_tensor,
            ) as all_gather,
        ):
            comm = comm_mod.MluCommunicator(group=group)
            x = torch.tensor([[1, 2, 3], [4, 5, 6]], dtype=torch.float32)
            reduced = comm.all_reduce(x)
            gathered = comm.all_gather(x, dim=-1)

        self.assertIs(reduced, x)
        all_reduce.assert_called_once_with(x, group=group)
        all_gather.assert_called_once()
        self.assertEqual(
            gathered.tolist(),
            [[1, 2, 3, 11, 12, 13], [4, 5, 6, 14, 15, 16]],
        )


class TestMLURotaryCacheTransform(CustomTestCase):
    def test_transform_cache_interleaved_layout(self):
        from sglang.srt.layers.rotary_embedding.base import _transform_cache

        cache = torch.arange(8, dtype=torch.float32).reshape(1, 8)
        cos, sin = _transform_cache(cache, is_neox_style=False)

        self.assertEqual(cos.tolist(), [[0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0]])
        self.assertEqual(sin.tolist(), [[4.0, 4.0, 5.0, 5.0, 6.0, 6.0, 7.0, 7.0]])

    def test_transform_cache_neox_layout(self):
        from sglang.srt.layers.rotary_embedding.base import _transform_cache

        cache = torch.arange(8, dtype=torch.float32).reshape(1, 8)
        cos, sin = _transform_cache(cache, is_neox_style=True)

        self.assertEqual(cos.tolist(), [[0.0, 1.0, 2.0, 3.0, 0.0, 1.0, 2.0, 3.0]])
        self.assertEqual(sin.tolist(), [[4.0, 5.0, 6.0, 7.0, 4.0, 5.0, 6.0, 7.0]])


if __name__ == "__main__":
    unittest.main()
