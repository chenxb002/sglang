import unittest

from sglang.test.ci.ci_register import register_mlu_ci


register_mlu_ci(est_time=1, suite="pr-test-1-mlu")


class TestMLUCIFailureDemo(unittest.TestCase):
    def test_intentional_failure_reaches_github_actions(self):
        self.fail(
            "Intentional failure for the MLU CI negative-path demo; "
            "this verifies Jenkins test_fail propagation and log artifact upload."
        )


if __name__ == "__main__":
    unittest.main()
