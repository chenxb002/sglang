"""Eager MLU spec decode with a small self-draft model.

The identical target and draft make broken draft positions/cache lengths visible
as a drop in greedy acceptance. Attention numerical tests separately cover MLA.
Set MLU_SPEC_TEST_MODEL to use a local Qwen3-0.6B checkpoint.
"""

import os
import tempfile
import unittest

from transformers import DeepseekV3Config

import sglang as sgl
from sglang.test.ci.ci_register import register_mlu_ci
from sglang.test.test_utils import CustomTestCase

register_mlu_ci(est_time=240, suite="extra-test-1-mlu")


class TestMLUSpecDecode(CustomTestCase):
    attention_backend = "mlu"
    speculative_topk = 1
    num_draft_tokens = 4

    @classmethod
    def setUpClass(cls):
        model = os.environ.get("MLU_SPEC_TEST_MODEL", "Qwen/Qwen3-0.6B")
        cls.engine = sgl.Engine(
            model_path=model,
            device="mlu",
            attention_backend=cls.attention_backend,
            speculative_algorithm="STANDALONE",
            speculative_draft_model_path=model,
            speculative_num_steps=3,
            speculative_num_draft_tokens=cls.num_draft_tokens,
            speculative_eagle_topk=cls.speculative_topk,
            disable_cuda_graph=True,
            max_total_tokens=1024,
            context_length=512,
            max_running_requests=4,
            mem_fraction_static=0.9,
        )

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "engine", None) is not None:
            cls.engine.shutdown()

    def _generate(self, params):
        outputs = self.engine.generate(
            ["The capital of France is", "The first ten positive integers are"],
            sampling_params={"max_new_tokens": 32, "ignore_eos": True, **params},
        )
        for output in outputs:
            self.assertTrue(output["text"])
            self.assertEqual(output["meta_info"]["completion_tokens"], 32)
        return outputs

    def test_greedy_cache_reuse_and_acceptance(self):
        first = self._generate({"temperature": 0})
        second = self._generate({"temperature": 0})
        self.assertEqual([o["text"] for o in first], [o["text"] for o in second])
        for output in first:
            meta = output["meta_info"]
            self.assertGreater(meta["completion_tokens"] / meta["spec_verify_ct"], 2.5)

    def test_requests_finish_at_different_steps(self):
        outputs = self.engine.generate(
            ["The capital of France is", "Count from one to ten:"],
            sampling_params=[
                {"temperature": 0, "max_new_tokens": n, "ignore_eos": True}
                for n in (7, 29)
            ],
        )
        self.assertEqual(
            [o["meta_info"]["completion_tokens"] for o in outputs], [7, 29]
        )

    def test_sampled_verify(self):
        self._generate({"temperature": 0.8, "top_k": 20, "top_p": 0.9})


class TestMLUTritonTreeDecode(TestMLUSpecDecode):
    attention_backend = "triton"
    speculative_topk = 2
    num_draft_tokens = 7

    def test_mixed_sampling_and_cache_reuse(self):
        prompts = ["The capital of France is", "Count from one to ten:"]
        params = [
            {"temperature": 0, "max_new_tokens": 17, "ignore_eos": True},
            {
                "temperature": 0.8,
                "top_k": 20,
                "top_p": 0.9,
                "max_new_tokens": 29,
                "ignore_eos": True,
            },
        ]
        greedy = self.engine.generate(prompts[0], sampling_params=params[0])
        for _ in range(2):
            outputs = self.engine.generate(prompts, sampling_params=params)
            self.assertEqual(outputs[0]["text"], greedy["text"])
            self.assertEqual(
                [o["meta_info"]["completion_tokens"] for o in outputs], [17, 29]
            )
            for output in outputs:
                self.assertGreater(output["meta_info"]["spec_verify_ct"], 0)


class TestMLUMLAMTP(CustomTestCase):
    """Exercise real EAGLE hidden-state handoff with tiny random DeepSeek weights."""

    @classmethod
    def setUpClass(cls):
        cls.model_dir = tempfile.TemporaryDirectory(prefix="mlu-mla-mtp-")
        config = DeepseekV3Config(
            architectures=["DeepseekV3ForCausalLM"],
            hidden_size=256,
            intermediate_size=512,
            moe_intermediate_size=128,
            moe_layer_freq=1,
            scoring_func="sigmoid",
            topk_method="noaux_tc",
            rope_theta=10000,
            num_hidden_layers=1,
            num_attention_heads=8,
            num_key_value_heads=8,
            num_nextn_predict_layers=1,
            n_routed_experts=4,
            n_shared_experts=1,
            num_experts_per_tok=2,
            n_group=1,
            topk_group=1,
            first_k_dense_replace=1,
            kv_lora_rank=512,
            q_lora_rank=128,
            qk_nope_head_dim=128,
            qk_rope_head_dim=64,
            v_head_dim=128,
            vocab_size=1024,
            max_position_embeddings=512,
            rope_scaling=None,
            torch_dtype="bfloat16",
        )
        config.save_pretrained(cls.model_dir.name)
        cls.engine = sgl.Engine(
            model_path=cls.model_dir.name,
            load_format="dummy",
            skip_tokenizer_init=True,
            device="mlu",
            attention_backend="mlu",
            speculative_algorithm="EAGLE",
            speculative_num_steps=3,
            speculative_num_draft_tokens=4,
            speculative_eagle_topk=1,
            disable_cuda_graph=True,
            max_total_tokens=512,
            context_length=256,
            max_running_requests=4,
            mem_fraction_static=0.9,
        )

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "engine", None) is not None:
            cls.engine.shutdown()
        if getattr(cls, "model_dir", None) is not None:
            cls.model_dir.cleanup()

    def test_mtp_generate(self):
        outputs = self.engine.generate(
            input_ids=[[5, 7, 9], [11, 13, 15, 17]],
            sampling_params={
                "temperature": 0,
                "max_new_tokens": 16,
                "ignore_eos": True,
            },
        )
        for output in outputs:
            self.assertEqual(len(output["output_ids"]), 16)
            self.assertTrue(all(0 <= token < 1024 for token in output["output_ids"]))
            self.assertGreater(output["meta_info"]["spec_verify_ct"], 0)


if __name__ == "__main__":
    unittest.main()
