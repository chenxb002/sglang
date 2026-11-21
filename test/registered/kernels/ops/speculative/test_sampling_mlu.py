"""MLU linear-chain verification: rejection tails, bonus tokens and filtering."""

import unittest

import torch

from sglang.kernels.ops.sampling.renorm_mlu import top_k_renorm_prob, top_p_renorm_prob
from sglang.kernels.ops.speculative.sampling_mlu import (
    chain_speculative_sampling_target_only,
)
from sglang.kernels.ops.speculative.spec_tree import verify_tree_greedy_func
from sglang.test.ci.ci_register import register_mlu_ci
from sglang.test.test_utils import CustomTestCase

register_mlu_ci(est_time=30, suite="extra-test-1-mlu")


class TestMLUVerify(CustomTestCase):
    def test_accept_and_reject(self):
        # Rows reject immediately, after one draft, or accept the entire chain.
        candidates = torch.tensor([[0, 1, 2]] * 3, device="mlu")
        targets = torch.tensor([[3, 2, 4], [1, 3, 4], [1, 2, 4]], device="mlu")
        indices = torch.arange(9, device="mlu").reshape(3, 3)
        next_tokens = torch.tensor([[1, 2, -1]] * 3, device="mlu")
        siblings = torch.full_like(next_tokens, -1)
        expected_indices = torch.tensor([[0, -1, -1], [3, 4, -1], [6, 7, 8]])
        for greedy in (True, False):
            with self.subTest(greedy=greedy):
                predict = torch.zeros(9, device="mlu", dtype=torch.int32)
                accept_indices = torch.full((3, 3), -1, device="mlu", dtype=torch.int32)
                num_correct_drafts = torch.empty(3, device="mlu", dtype=torch.int32)
                if greedy:
                    verify_tree_greedy_func(
                        predict,
                        accept_indices,
                        num_correct_drafts,
                        candidates,
                        indices,
                        next_tokens,
                        siblings,
                        targets,
                    )
                else:
                    probs = torch.nn.functional.one_hot(targets, 5).float()
                    chain_speculative_sampling_target_only(
                        predicts=predict,
                        accept_index=accept_indices,
                        accept_token_num=num_correct_drafts,
                        candidates=candidates,
                        retrive_index=indices,
                        retrive_next_token=next_tokens,
                        retrive_next_sibling=siblings,
                        target_probs=probs,
                        draft_probs=torch.zeros_like(probs),
                        uniform_samples=torch.full((3, 3), 0.5, device="mlu"),
                        uniform_samples_for_final_sampling=torch.full(
                            (3,), 0.5, device="mlu"
                        ),
                    )
                torch.testing.assert_close(
                    accept_indices.cpu().long(), expected_indices
                )
                torch.testing.assert_close(
                    num_correct_drafts.cpu().long(), torch.arange(3)
                )
                for row, expected in enumerate(([3], [1, 3], [1, 2, 4])):
                    self.assertEqual(
                        predict[accept_indices[row, : len(expected)].long()].tolist(),
                        expected,
                    )

    def test_rejection_samples_residual_distribution(self):
        candidates = torch.tensor([[4, 0], [4, 0]], device="mlu")
        indices = torch.arange(4, device="mlu").reshape(2, 2)
        predict = torch.zeros(4, device="mlu", dtype=torch.int32)
        accept_indices = torch.full((2, 2), -1, device="mlu", dtype=torch.int32)
        num_correct_drafts = torch.empty(2, device="mlu", dtype=torch.int32)
        probs = torch.tensor([[[0.6, 0.3, 0.1], [0.2, 0.3, 0.5]]] * 2, device="mlu")
        chain_speculative_sampling_target_only(
            predicts=predict,
            accept_index=accept_indices,
            accept_token_num=num_correct_drafts,
            candidates=candidates,
            retrive_index=indices,
            retrive_next_token=torch.tensor([[1, -1]] * 2, device="mlu"),
            retrive_next_sibling=torch.full_like(indices, -1),
            target_probs=probs,
            draft_probs=torch.zeros_like(probs),
            uniform_samples=torch.full((2, 2), 0.9, device="mlu"),
            uniform_samples_for_final_sampling=torch.tensor([0.1, 0.9], device="mlu"),
        )
        self.assertEqual(num_correct_drafts.tolist(), [0, 0])
        self.assertEqual(accept_indices.tolist(), [[0, -1], [2, -1]])
        # The rejected token is removed: the residual is [0, .75, .25].
        self.assertEqual(predict[[0, 2]].tolist(), [1, 2])

    def test_acceptance_probability_boundaries(self):
        # (draft probability, coin, single threshold, expected correct drafts)
        # Zero mass can come from grammar masking or top-k/top-p filtering.
        for probability, coin, threshold, expected in (
            (0.0, 0.5, 0.0, 0),
            (0.0, 0.0, 1.0, 0),
            (0.5, 0.5, 1.0, 0),  # Equality rejects, matching the CUDA kernel.
            (0.5, 0.25, 1.0, 1),
            (0.5, 0.75, 0.0, 1),  # Relaxed threshold still accepts positive mass.
        ):
            with self.subTest(probability=probability, coin=coin, threshold=threshold):
                indices = torch.tensor([[0, 1]], device="mlu")
                predict = torch.zeros(2, device="mlu", dtype=torch.int32)
                accept_indices = torch.full((1, 2), -1, device="mlu", dtype=torch.int32)
                num_correct_drafts = torch.empty(1, device="mlu", dtype=torch.int32)
                probs = torch.tensor(
                    [[[1 - probability, probability], [1.0, 0.0]]], device="mlu"
                )
                chain_speculative_sampling_target_only(
                    predicts=predict,
                    accept_index=accept_indices,
                    accept_token_num=num_correct_drafts,
                    candidates=torch.tensor([[0, 1]], device="mlu"),
                    retrive_index=indices,
                    retrive_next_token=torch.tensor([[1, -1]], device="mlu"),
                    retrive_next_sibling=torch.full_like(indices, -1),
                    target_probs=probs,
                    draft_probs=torch.zeros_like(probs),
                    uniform_samples=torch.full((1, 2), coin, device="mlu"),
                    uniform_samples_for_final_sampling=torch.tensor(
                        [0.5], device="mlu"
                    ),
                    threshold_single=threshold,
                )
                self.assertEqual(num_correct_drafts.item(), expected)
                self.assertEqual(accept_indices.tolist(), [[0, 1 if expected else -1]])
                self.assertEqual(predict[0].item(), expected)

    def test_filtering(self):
        probs = torch.tensor([[0.6, 0.3, 0.1]] * 2, device="mlu")
        topk = top_k_renorm_prob(probs, torch.tensor([1, 1 << 30], device="mlu"))
        topp = top_p_renorm_prob(probs, torch.tensor([0.8, 1.0], device="mlu"))
        torch.testing.assert_close(
            topk.cpu(), torch.tensor([[1.0, 0.0, 0.0], [0.6, 0.3, 0.1]])
        )
        torch.testing.assert_close(
            topp.cpu(), torch.tensor([[2 / 3, 1 / 3, 0.0], [0.6, 0.3, 0.1]])
        )


if __name__ == "__main__":
    unittest.main()
