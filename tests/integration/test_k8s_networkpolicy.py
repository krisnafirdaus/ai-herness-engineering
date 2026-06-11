"""Deny-all NetworkPolicy is actually ENFORCED against sandbox pods.

Opt-in: HARNESS_TEST_K8S=1 + HARNESS_TEST_K8S_NETPOL=1 and a cluster whose
CNI enforces NetworkPolicy (Calico/Cilium). kind's default kindnet accepts
the policy object but does not enforce it — this test would rightly FAIL
there, hence the separate opt-in:

    kind create cluster --config kind-calico.yaml   # disableDefaultCNI
    kubectl apply -f .../calico.yaml
    kind load docker-image harness-sandbox:latest
    HARNESS_TEST_K8S=1 HARNESS_TEST_K8S_NETPOL=1 pytest tests/integration/test_k8s_networkpolicy.py
"""
import os
import pathlib
import time

import pytest

from integration.conftest import k8s_enabled

pytestmark = [
    pytest.mark.integration, pytest.mark.k8s,
    pytest.mark.skipif(
        not (os.environ.get("HARNESS_TEST_K8S_NETPOL") == "1" and k8s_enabled()),
        reason="HARNESS_TEST_K8S_NETPOL!=1 (needs a policy-enforcing CNI — "
               "Calico/Cilium; kind's default kindnet does not enforce)"),
]

POLICY_FILE = (pathlib.Path(__file__).resolve().parents[2]
               / "infra" / "k8s" / "sandbox-networkpolicy.yaml")
# IP literal on purpose: deny-all also kills DNS, which would mask the result.
PROBE = ("python3 -c \"import socket; "
         "socket.create_connection(('1.1.1.1', 443), timeout=4); "
         "print('EGRESS-OK')\"")


def _policy_body():
    import yaml

    return yaml.safe_load(POLICY_FILE.read_text())


def test_deny_all_blocks_sandbox_egress(tmp_path):
    """Control first (egress works without the policy), then enforcement."""
    from kubernetes import client

    from src.config import settings
    from src.sandbox.k8s_runner import K8sSandbox

    net = client.NetworkingV1Api()
    ns = settings.k8s_namespace
    body = _policy_body()
    name = body["metadata"]["name"]

    # Start from a clean slate so the control phase is meaningful.
    try:
        net.delete_namespaced_network_policy(name, ns)
        time.sleep(2)
    except client.ApiException as exc:
        if exc.status != 404:
            raise

    sb = K8sSandbox(str(tmp_path / "repo"), settings.sandbox_image)
    try:
        # Control: without the policy the pod can reach the outside world —
        # proves the CNI/NAT path works, so a later failure means "blocked",
        # not "cluster has no internet".
        r = sb.exec(PROBE, timeout=60)
        assert r.ok and "EGRESS-OK" in r.stdout, (
            f"control failed — cluster has no egress at all: {r.stderr}")

        net.create_namespaced_network_policy(ns, body)
        try:
            # Enforcement may take a moment to program; poll until blocked.
            deadline = time.time() + 30
            blocked = False
            while time.time() < deadline and not blocked:
                r = sb.exec(PROBE, timeout=60)
                blocked = not r.ok
                if not blocked:
                    time.sleep(2)
            assert blocked, "egress still succeeds with deny-all applied"
        finally:
            net.delete_namespaced_network_policy(name, ns)
    finally:
        sb.teardown()
