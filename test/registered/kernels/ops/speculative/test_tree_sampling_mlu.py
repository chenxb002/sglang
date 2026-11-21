"""MLU tree verification: sibling rejection, residuals and target marginals."""

import unittest

import torch

from sglang.kernels.ops.speculative.target_only_sampling import (
    tree_speculative_sampling_target_only_triton,
)
from sglang.test.ci.ci_register import register_mlu_ci
from sglang.test.test_utils import CustomTestCase

register_mlu_ci(est_time=40, suite="extra-test-1-mlu")


class TestMLUTreeSampling(CustomTestCase):
    def _run(self, probs, coins, final_coins, candidates, children, siblings, depth):
        bs, width = candidates.shape
        device = "mlu"
        predicts = torch.full((bs * width,), -1, dtype=torch.int32, device=device)
        indices = torch.full((bs, depth), -1, dtype=torch.int32, device=device)
        counts = torch.empty(bs, dtype=torch.int32, device=device)
        tree_speculative_sampling_target_only_triton(
            predicts=predicts,
            accept_index=indices,
            accept_token_num=counts,
            candidates=candidates.to(device),
            retrive_index=torch.arange(bs * width, device=device).reshape(bs, width),
            retrive_next_token=children.to(device),
            retrive_next_sibling=siblings.to(device),
            uniform_samples=coins.to(device),
            uniform_samples_for_final_sampling=final_coins.to(device),
            target_probs=probs.to(device),
            draft_probs=torch.zeros_like(probs, device=device),
        )
        return predicts.cpu(), indices.cpu(), counts.cpu()

    def test_sibling_and_deep_rejection(self):
        # Root children are 1/2; their children are 3/4, respectively.
        # Cover acceptance after rejecting a sibling, rejection after acceptance,
        # complete rejection, and complete acceptance, with vocab chunk crossings.
        for vocab in (7, 4103, 151936):
            with self.subTest(vocab=vocab):
                probs = torch.zeros((4, 5, vocab))
                probs[:, :, [0, 1, 2, 3, vocab - 1]] = torch.tensor(
                    [0.125, 0.25, 0.25, 0.125, 0.25]
                )
                # Different leaf policies catch sampling the bonus from the
                # root or from a sibling instead of the last accepted node.
                probs[:, 3:] = 0
                probs[:, 3, 2] = 1
                probs[:, 4, 0] = 1
                candidates = torch.tensor([[0, 1, 2, 3, vocab - 1]] * 4)
                children = torch.tensor([[1, 3, 4, -1, -1]] * 4)
                siblings = torch.tensor([[-1, 2, -1, -1, -1]] * 4)
                coins = torch.tensor(
                    [
                        [0.375, 0.5, 0.125, 0.5, 0.5],
                        [0.125, 0.875, 0.5, 0.5, 0.5],
                        [0.875, 0.5, 0.5, 0.5, 0.5],
                        [0.125, 0.0625, 0.5, 0.5, 0.5],
                    ]
                )
                predicts, indices, counts = self._run(
                    probs,
                    coins,
                    torch.full((4,), 0.875),
                    candidates,
                    children,
                    siblings,
                    3,
                )
                self.assertEqual(counts.tolist(), [2, 1, 0, 2])
                self.assertEqual(
                    indices.tolist(),
                    [[0, 2, 4], [5, 6, -1], [10, -1, -1], [15, 16, 18]],
                )
                expected = [
                    [2, vocab - 1, 0],
                    [1, vocab - 1],
                    [vocab - 1],
                    [1, 3, 2],
                ]
                for row, tokens in enumerate(expected):
                    self.assertEqual(
                        predicts[indices[row, : len(tokens)].long()].tolist(), tokens
                    )

    def test_tree_preserves_target_distribution(self):
        # Cartesian midpoint coins integrate both branch acceptance and residual
        # sampling. A chain-only verifier would miss sibling 2 and change paths.
        grid = (torch.arange(64, dtype=torch.float32) + 0.5) / 64
        accept_coins, final_coins = torch.meshgrid(grid, grid, indexing="ij")
        bs = accept_coins.numel()
        probs = torch.tensor([0.125, 0.25, 0.375, 0.25]).repeat(bs, 3, 1)
        coins = torch.full((bs, 3), 0.5)
        coins[:, 0] = accept_coins.flatten()
        predicts, indices, counts = self._run(
            probs,
            coins,
            final_coins.flatten(),
            torch.tensor([[0, 1, 2]]).repeat(bs, 1),
            torch.tensor([[1, -1, -1]]).repeat(bs, 1),
            torch.tensor([[-1, 2, -1]]).repeat(bs, 1),
            2,
        )
        frequencies = (
            torch.bincount(predicts[indices[:, 0].long()].long(), minlength=4) / bs
        )
        torch.testing.assert_close(frequencies, probs[0, 0], atol=0.005, rtol=0)
        self.assertEqual(counts.sum().item(), int(bs * 0.625))
        self.assertEqual(
            ((counts > 0) & (indices[:, 1] % 3 == 2)).sum().item(), int(bs * 0.375)
        )

    def test_zero_probability_candidate_never_accepted(self):
        predicts, indices, counts = self._run(
            torch.tensor([[[0.0, 0.0, 1.0]] * 3]),
            torch.zeros(1, 3),
            torch.zeros(1),
            torch.tensor([[0, 1, 2]]),
            torch.tensor([[1, -1, -1]]),
            torch.tensor([[-1, 2, -1]]),
            2,
        )
        self.assertEqual(counts.tolist(), [1])
        self.assertEqual(indices.tolist(), [[0, 2]])
        self.assertEqual(predicts[[0, 2]].tolist(), [2, 2])


if __name__ == "__main__":
    unittest.main()
