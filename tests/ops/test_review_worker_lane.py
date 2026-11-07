from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_review_lane_is_registered_and_template_selects_instance_claim_mode() -> None:
    units = (ROOT / "deploy/voyn-aicc-worker-lanes.conf").read_text()
    lanes = (ROOT / "deploy/aicc/worker-lanes").read_text()
    template = (ROOT / "deploy/systemd/voyn-aicc-worker@.service").read_text()

    assert "voyn-aicc-worker@review.service" in units.splitlines()
    assert "review" in lanes.splitlines()
    assert "Environment=AICC_WORKER_CLAIM_MODE=%i" in template
