from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Union

import torch
from sglang.srt.model_executor.cuda_graph_runner import CudaGraphRunner
from torch.profiler import ProfilerActivity, profile

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sglang.srt.model_executor.model_runner import ModelRunner


class MLUGraphRunner(CudaGraphRunner):
    """A MLUGraphRunner runs the forward pass of a model with mlu graph and torch.compile."""

    def __init__(self, model_runner: "ModelRunner"):
        super().__init__(model_runner)
        self.update_attr_name = None
        self.update_attr_type = None
        self.model_runner = model_runner
        self._init_arch_map()

    def _init_arch_map(self):
        self.attr_name: Dict[str, str] = {}
        self.attr_type: Dict[str, Union[list, torch.Tensor]] = {}

    def _create_device_graph(self):
        return torch.mlu.MLUGraph()

    def _capture_graph(self, graph, pool, stream, run_once_fn):
        with torch.mlu.graph(
            graph,
            pool=pool,
            stream=stream,
            # auto_dispatch_capture=True,
        ):
            out = run_once_fn()
        return out

    def _cache_loc_dtype(self):
        return torch.int32

    def _init_profile_context_and_memory_record(self):
        profile_context = profile(
            activities=[ProfilerActivity.CPU, ProfilerActivity.MLU],
            record_shapes=True,
        )
        torch.mlu.memory._record_memory_history()
        return profile_context

    def _post_process_after_profile(self, prof_context):
        torch.mlu.memory._dump_snapshot("mlu_graph_runner_memory_usage.pickle")
        torch.mlu.memory_record_memory_history(enabled=None)
        log_message = (
            "Sorted by MLU Time:\n"
            + prof_context.key_averages(group_by_input_shape=True).table(
                sort_by="mlu_time_total", row_limit=10
            )
            + "\n\nSorted by CPU Time:\n"
            + prof_context.key_averages(group_by_input_shape=True).table(
                sort_by="cpu_time_total", row_limit=10
            )
            + "\n\nMemory Usage is saved to mlu_graph_runner_memory_usage.pickle\n"
        )
        logger.info(log_message)
