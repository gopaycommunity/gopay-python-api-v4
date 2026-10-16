from __future__ import annotations

import copy
import pickle

import pytest

from gopay_payments import ErrorCode, GoPayError, GoPayHTTPError, GoPaySDKError


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            GoPayHTTPError(
                409,
                {"errors": [{"error_code": 409}]},
                method="DELETE",
                endpoint="/payments/{id}",
            ),
            id="http",
        ),
        pytest.param(
            GoPaySDKError(
                "[GoPaySDK] Charge failed",
                error_code=ErrorCode.CHARGE_FAILED,
                charge_state={"state": "FAILED", "fail_reason": "DECLINED"},
            ),
            id="sdk",
        ),
    ],
)
def test_errors_survive_pickle_and_copy(error: GoPayError) -> None:
    # A Celery task or a ProcessPoolExecutor pickles the error to hand it back.
    clones = [
        pickle.loads(pickle.dumps(error)),  # noqa: S301 — our own object, round-tripped
        copy.copy(error),
        copy.deepcopy(error),
    ]
    for clone in clones:
        assert type(clone) is type(error)
        assert str(clone) == str(error)
        assert vars(clone) == vars(error)
