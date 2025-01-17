import logging

import pytest

from maasserver.enum import NODE_STATUS


@pytest.mark.django_db
def test_node_mark_failed_deployment_logs_failure(factory, caplog):
    node = factory.make_Node(
        status=NODE_STATUS.DEPLOYING, with_boot_disk=False
    )
    with caplog.at_level(logging.DEBUG):
        node.mark_failed()
    assert node.status == NODE_STATUS.FAILED_DEPLOYMENT
    record_tuple = (
        "maas.node",
        logging.DEBUG,
        f"Node '{node.hostname}' failed deployment",
    )

    assert record_tuple in caplog.record_tuples
