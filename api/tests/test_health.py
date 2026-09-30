"""kobo_api.health: the cluster's verdict on whether a ready agent runs."""

from datetime import UTC, datetime

from kobo_api.health import DOWN, UP, probe_health
from tests.fakes import FAKE_DEPLOYMENT_STATUS_SETTLED, FakeKubectl

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

CRASHED = {
    "metadata": {"generation": 3},
    "spec": {"replicas": 1},
    "status": {"observedGeneration": 3, "updatedReplicas": 1, "unavailableReplicas": 1},
}


def test_a_settled_deployment_is_up():
    kubectl = FakeKubectl(deployment_status={("clio", "clio"): FAKE_DEPLOYMENT_STATUS_SETTLED})

    verdict = probe_health(kubectl, "clio", now=NOW)

    assert verdict.state == UP
    assert verdict.reason is None
    assert verdict.checked_at == NOW.isoformat()


def test_no_available_pods_is_down_with_the_count_and_the_worst_container():
    """lumbot, 2026-09-30: four days of CrashLoopBackOff under a green Ready."""
    kubectl = FakeKubectl(deployment_status={("lumbot", "lumbot"): CRASHED})
    kubectl.pods = {
        "lumbot": [
            {
                "status": {
                    "containerStatuses": [
                        {
                            "name": "app",
                            "restartCount": 1393,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ]
                }
            }
        ]
    }

    verdict = probe_health(kubectl, "lumbot", now=NOW)

    assert verdict.state == DOWN
    assert verdict.reason == "0 of 1 running; restarted 1393 times, CrashLoopBackOff"


def test_an_unreadable_cluster_is_not_a_verdict():
    kubectl = FakeKubectl(deployment_status_error="connection refused")

    verdict = probe_health(kubectl, "clio", now=NOW)

    assert verdict.state == UP
    assert verdict.reason is None
