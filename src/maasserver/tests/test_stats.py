# Copyright 2014-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver.stats."""


import base64
import json

from django.db import transaction
import requests as requests_module
from twisted.application.internet import TimerService
from twisted.internet.defer import fail

from maasserver import stats
from maasserver.enum import IPADDRESS_TYPE, IPRANGE_TYPE, NODE_STATUS
from maasserver.models import (
    BootResourceFile,
    Config,
    Fabric,
    OwnerData,
    Space,
    Subnet,
    VLAN,
)
from maasserver.stats import (
    get_custom_images_deployed_stats,
    get_custom_images_uploaded_stats,
    get_maas_stats,
    get_machine_stats,
    get_machines_by_architecture,
    get_request_params,
    get_vm_hosts_stats,
    get_workload_annotations_stats,
    make_maas_user_agent_request,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maastesting.matchers import MockCalledOnce, MockNotCalled
from maastesting.testcase import MAASTestCase
from maastesting.twisted import extract_result
from provisioningserver.utils.twisted import asynchronous


class TestMAASStats(MAASServerTestCase):
    def make_pod(
        self,
        cpu=0,
        mem=0,
        cpu_over_commit=1,
        mem_over_commit=1,
        pod_type="virsh",
    ):
        # Make one pod
        zone = factory.make_Zone()
        pool = factory.make_ResourcePool()
        ip = factory.make_ipv4_address()
        power_parameters = {
            "power_address": "qemu+ssh://%s/system" % ip,
            "power_pass": "pass",
        }
        return factory.make_Pod(
            pod_type=pod_type,
            zone=zone,
            pool=pool,
            cores=cpu,
            memory=mem,
            cpu_over_commit_ratio=cpu_over_commit,
            memory_over_commit_ratio=mem_over_commit,
            parameters=power_parameters,
        )

    def test_get_machines_by_architecture(self):
        arches = [
            "amd64/generic",
            "s390x/generic",
            "ppc64el/generic",
            "arm64/generic",
            "i386/generic",
        ]
        for arch in arches:
            factory.make_Machine(architecture=arch)
        stats = get_machines_by_architecture()
        compare = {"amd64": 1, "i386": 1, "arm64": 1, "ppc64el": 1, "s390x": 1}
        self.assertEqual(stats, compare)

    def test_get_vm_hosts_stats(self):
        pod1 = self.make_pod(
            cpu=10, mem=100, cpu_over_commit=2, mem_over_commit=3
        )
        pod2 = self.make_pod(
            cpu=20, mem=200, cpu_over_commit=3, mem_over_commit=2
        )

        total_cores = pod1.cores + pod2.cores
        total_memory = pod1.memory + pod2.memory
        over_cores = (
            pod1.cores * pod1.cpu_over_commit_ratio
            + pod2.cores * pod2.cpu_over_commit_ratio
        )
        over_memory = (
            pod1.memory * pod1.memory_over_commit_ratio
            + pod2.memory * pod2.memory_over_commit_ratio
        )

        stats = get_vm_hosts_stats()
        compare = {
            "vm_hosts": 2,
            "vms": 0,
            "available_resources": {
                "cores": total_cores,
                "memory": total_memory,
                "over_cores": over_cores,
                "over_memory": over_memory,
                "storage": 0,
            },
            "utilized_resources": {"cores": 0, "memory": 0, "storage": 0},
        }
        self.assertEqual(compare, stats)

    def test_get_vm_hosts_stats_filtered(self):
        self.make_pod(cpu=10, mem=100, pod_type="lxd")
        self.make_pod(cpu=20, mem=200, pod_type="virsh")

        stats = get_vm_hosts_stats(power_type="lxd")
        compare = {
            "vm_hosts": 1,
            "vms": 0,
            "available_resources": {
                "cores": 10,
                "memory": 100,
                "over_cores": 10.0,
                "over_memory": 100.0,
                "storage": 0,
            },
            "utilized_resources": {"cores": 0, "memory": 0, "storage": 0},
        }
        self.assertEqual(compare, stats)

    def test_get_vm_hosts_stats_machine_usage(self):
        lxd_vm_host = self.make_pod(cpu=10, mem=100, pod_type="lxd")
        lxd_machine = factory.make_Machine(
            bmc=lxd_vm_host, cpu_count=1, memory=10
        )
        factory.make_VirtualMachine(bmc=lxd_vm_host, machine=lxd_machine)
        virsh_vm_host = self.make_pod(cpu=20, mem=200, pod_type="virsh")
        virsh_machine = factory.make_Machine(
            bmc=virsh_vm_host, cpu_count=2, memory=20
        )
        factory.make_VirtualMachine(bmc=virsh_vm_host, machine=virsh_machine)

        stats = get_vm_hosts_stats(power_type="lxd")
        compare = {
            "vm_hosts": 1,
            "vms": 1,
            "available_resources": {
                "cores": 10,
                "memory": 100,
                "over_cores": 10.0,
                "over_memory": 100.0,
                "storage": 0,
            },
            "utilized_resources": {"cores": 1, "memory": 10, "storage": 0},
        }
        self.assertEqual(compare, stats)

    def test_get_vm_hosts_stats_no_pod(self):
        self.assertEqual(
            get_vm_hosts_stats(),
            {
                "vm_hosts": 0,
                "vms": 0,
                "available_resources": {
                    "cores": 0,
                    "memory": 0,
                    "storage": 0,
                    "over_cores": 0,
                    "over_memory": 0,
                },
                "utilized_resources": {
                    "cores": 0,
                    "memory": 0,
                    "storage": 0,
                },
            },
        )

    def test_get_maas_stats(self):
        # Make one component of everything
        factory.make_RegionRackController()
        factory.make_RegionController()
        factory.make_RackController()
        factory.make_Machine(cpu_count=2, memory=200, status=NODE_STATUS.READY)
        factory.make_Machine(status=NODE_STATUS.READY)
        factory.make_Machine(status=NODE_STATUS.NEW)
        for _ in range(4):
            factory.make_Machine(status=NODE_STATUS.ALLOCATED)
        factory.make_Machine(
            cpu_count=3, memory=100, status=NODE_STATUS.FAILED_DEPLOYMENT
        )
        factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        deployed_machine = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        OwnerData.objects.set_owner_data(deployed_machine, {"foo": "bar"})
        factory.make_Device()
        factory.make_Device()
        self.make_pod(cpu=10, mem=100, pod_type="lxd")
        self.make_pod(cpu=20, mem=200, pod_type="virsh")

        subnets = Subnet.objects.all()
        v4 = [net for net in subnets if net.get_ip_version() == 4]
        v6 = [net for net in subnets if net.get_ip_version() == 6]

        stats = get_maas_stats()
        machine_stats = get_machine_stats()

        # Due to floating point calculation subtleties, sometimes the value the
        # database returns is off by one compared to the value Python
        # calculates, so just get it directly from the database for the test.
        total_storage = machine_stats["total_storage"]

        expected = {
            "controllers": {"regionracks": 1, "regions": 1, "racks": 1},
            "nodes": {"machines": 10, "devices": 2},
            "machine_stats": {
                "total_cpu": 5,
                "total_mem": 300,
                "total_storage": total_storage,
            },
            "machine_status": {
                "new": 1,
                "ready": 2,
                "allocated": 4,
                "deployed": 2,
                "commissioning": 0,
                "testing": 0,
                "deploying": 0,
                "failed_deployment": 1,
                "failed_commissioning": 0,
                "failed_testing": 0,
                "broken": 0,
            },
            "network_stats": {
                "spaces": Space.objects.count(),
                "fabrics": Fabric.objects.count(),
                "vlans": VLAN.objects.count(),
                "subnets_v4": len(v4),
                "subnets_v6": len(v6),
            },
            "vm_hosts": {
                "lxd": {
                    "vm_hosts": 1,
                    "vms": 0,
                    "available_resources": {
                        "cores": 10,
                        "memory": 100,
                        "over_cores": 10.0,
                        "over_memory": 100.0,
                        "storage": 0,
                    },
                    "utilized_resources": {
                        "cores": 0,
                        "memory": 0,
                        "storage": 0,
                    },
                },
                "virsh": {
                    "vm_hosts": 1,
                    "vms": 0,
                    "available_resources": {
                        "cores": 20,
                        "memory": 200,
                        "over_cores": 20.0,
                        "over_memory": 200.0,
                        "storage": 0,
                    },
                    "utilized_resources": {
                        "cores": 0,
                        "memory": 0,
                        "storage": 0,
                    },
                },
            },
            "workload_annotations": {
                "annotated_machines": 1,
                "total_annotations": 1,
                "unique_keys": 1,
                "unique_values": 1,
            },
        }
        self.assertEqual(stats, expected)

    def test_get_workload_annotations_stats_machines(self):
        machine1 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        machine2 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        machine3 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        factory.make_Machine(status=NODE_STATUS.DEPLOYED)

        OwnerData.objects.set_owner_data(
            machine1, {"key1": "value1", "key2": "value2"}
        )
        OwnerData.objects.set_owner_data(machine2, {"key1": "value1"})
        OwnerData.objects.set_owner_data(machine3, {"key2": "value2"})

        workload_stats = get_workload_annotations_stats()
        self.assertEqual(3, workload_stats["annotated_machines"])

    def test_get_workload_annotations_stats_keys(self):
        machine1 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        machine2 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        machine3 = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
        factory.make_Machine(status=NODE_STATUS.DEPLOYED)

        OwnerData.objects.set_owner_data(
            machine1, {"key1": "value1", "key2": "value2"}
        )
        OwnerData.objects.set_owner_data(machine2, {"key1": "value3"})
        OwnerData.objects.set_owner_data(machine3, {"key2": "value2"})

        workload_stats = get_workload_annotations_stats()
        self.assertEqual(4, workload_stats["total_annotations"])
        self.assertEqual(2, workload_stats["unique_keys"])
        self.assertEqual(3, workload_stats["unique_values"])

    def test_get_maas_stats_no_machines(self):
        expected = {
            "controllers": {"regionracks": 0, "regions": 0, "racks": 0},
            "nodes": {"machines": 0, "devices": 0},
            "machine_stats": {
                "total_cpu": 0,
                "total_mem": 0,
                "total_storage": 0,
            },
            "machine_status": {
                "new": 0,
                "ready": 0,
                "allocated": 0,
                "deployed": 0,
                "commissioning": 0,
                "testing": 0,
                "deploying": 0,
                "failed_deployment": 0,
                "failed_commissioning": 0,
                "failed_testing": 0,
                "broken": 0,
            },
            "network_stats": {
                "spaces": 0,
                "fabrics": Fabric.objects.count(),
                "vlans": VLAN.objects.count(),
                "subnets_v4": 0,
                "subnets_v6": 0,
            },
            "vm_hosts": {
                "lxd": {
                    "vm_hosts": 0,
                    "vms": 0,
                    "available_resources": {
                        "cores": 0,
                        "memory": 0,
                        "over_cores": 0.0,
                        "over_memory": 0.0,
                        "storage": 0,
                    },
                    "utilized_resources": {
                        "cores": 0,
                        "memory": 0,
                        "storage": 0,
                    },
                },
                "virsh": {
                    "vm_hosts": 0,
                    "vms": 0,
                    "available_resources": {
                        "cores": 0,
                        "memory": 0,
                        "over_cores": 0.0,
                        "over_memory": 0.0,
                        "storage": 0,
                    },
                    "utilized_resources": {
                        "cores": 0,
                        "memory": 0,
                        "storage": 0,
                    },
                },
            },
            "workload_annotations": {
                "annotated_machines": 0,
                "total_annotations": 0,
                "unique_keys": 0,
                "unique_values": 0,
            },
        }
        self.assertEqual(get_maas_stats(), expected)

    def test_get_request_params_returns_params(self):
        factory.make_RegionRackController()
        params = {
            "data": base64.b64encode(
                json.dumps(json.dumps(get_maas_stats())).encode()
            ).decode()
        }
        self.assertEqual(params, get_request_params())

    def test_make_user_agent_request(self):
        factory.make_RegionRackController()
        mock = self.patch(requests_module, "get")
        make_maas_user_agent_request()
        self.assertThat(mock, MockCalledOnce())

    def test_get_custom_static_images_uploaded_stats(self):
        for _ in range(0, 2):
            factory.make_usable_boot_resource(
                name="custom/%s" % factory.make_name("name"),
                base_image="ubuntu/focal",
            ),
        factory.make_usable_boot_resource(
            name="custom/%s" % factory.make_name("name"),
            base_image="ubuntu/bionic",
        ),
        stats = get_custom_images_uploaded_stats()
        total = 0
        for stat in stats:
            total += stat["count"]
        expected_total = (
            BootResourceFile.objects.exclude(
                resource_set__resource__base_image__isnull=True,
                resource_set__resource__base_image="",
            )
            .distinct()
            .count()
        )
        self.assertEqual(total, expected_total)

    def test_get_custom_static_images_deployed_stats(self):
        for _ in range(0, 2):
            machine = factory.make_Machine(status=NODE_STATUS.DEPLOYED)
            machine.osystem = "custom"
            machine.distro_series = factory.make_name("name")
            machine.save()
        self.assertEqual(get_custom_images_deployed_stats(), 2)


class TestGetSubnetsUtilisationStats(MAASServerTestCase):
    def test_stats_totals(self):
        factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        factory.make_Subnet(cidr="::1/128", gateway_ip="")
        self.assertEqual(
            stats.get_subnets_utilisation_stats(),
            {
                "1.2.0.0/16": {
                    "available": 2 ** 16 - 3,
                    "dynamic_available": 0,
                    "dynamic_used": 0,
                    "reserved_available": 0,
                    "reserved_used": 0,
                    "static": 0,
                    "unavailable": 1,
                },
                "::1/128": {
                    "available": 1,
                    "dynamic_available": 0,
                    "dynamic_used": 0,
                    "reserved_available": 0,
                    "reserved_used": 0,
                    "static": 0,
                    "unavailable": 0,
                },
            },
        )

    def test_stats_dynamic(self):
        subnet = factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.11",
            end_ip="1.2.0.20",
            alloc_type=IPRANGE_TYPE.DYNAMIC,
        )
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.51",
            end_ip="1.2.0.60",
            alloc_type=IPRANGE_TYPE.DYNAMIC,
        )
        factory.make_StaticIPAddress(
            ip="1.2.0.15", alloc_type=IPADDRESS_TYPE.DHCP, subnet=subnet
        )
        factory.make_StaticIPAddress(
            ip="1.2.0.52", alloc_type=IPADDRESS_TYPE.DHCP, subnet=subnet
        )
        self.assertEqual(
            stats.get_subnets_utilisation_stats(),
            {
                "1.2.0.0/16": {
                    "available": 2 ** 16 - 23,
                    "dynamic_available": 18,
                    "dynamic_used": 2,
                    "reserved_available": 0,
                    "reserved_used": 0,
                    "static": 0,
                    "unavailable": 21,
                }
            },
        )

    def test_stats_reserved(self):
        subnet = factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.11",
            end_ip="1.2.0.20",
            alloc_type=IPRANGE_TYPE.RESERVED,
        )
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.51",
            end_ip="1.2.0.60",
            alloc_type=IPRANGE_TYPE.RESERVED,
        )
        factory.make_StaticIPAddress(
            ip="1.2.0.15",
            alloc_type=IPADDRESS_TYPE.USER_RESERVED,
            subnet=subnet,
        )
        self.assertEqual(
            stats.get_subnets_utilisation_stats(),
            {
                "1.2.0.0/16": {
                    "available": 2 ** 16 - 23,
                    "dynamic_available": 0,
                    "dynamic_used": 0,
                    "reserved_available": 19,
                    "reserved_used": 1,
                    "static": 0,
                    "unavailable": 21,
                }
            },
        )

    def test_stats_static(self):
        subnet = factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        for n in (10, 20, 30):
            factory.make_StaticIPAddress(
                ip="1.2.0.{}".format(n),
                alloc_type=IPADDRESS_TYPE.STICKY,
                subnet=subnet,
            )
        self.assertEqual(
            stats.get_subnets_utilisation_stats(),
            {
                "1.2.0.0/16": {
                    "available": 2 ** 16 - 6,
                    "dynamic_available": 0,
                    "dynamic_used": 0,
                    "reserved_available": 0,
                    "reserved_used": 0,
                    "static": 3,
                    "unavailable": 4,
                }
            },
        )

    def test_stats_all(self):
        subnet = factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.11",
            end_ip="1.2.0.20",
            alloc_type=IPRANGE_TYPE.DYNAMIC,
        )
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.51",
            end_ip="1.2.0.70",
            alloc_type=IPRANGE_TYPE.RESERVED,
        )
        factory.make_StaticIPAddress(
            ip="1.2.0.12", alloc_type=IPADDRESS_TYPE.DHCP, subnet=subnet
        )
        for n in (60, 61):
            factory.make_StaticIPAddress(
                ip="1.2.0.{}".format(n),
                alloc_type=IPADDRESS_TYPE.USER_RESERVED,
                subnet=subnet,
            )
        for n in (80, 90, 100):
            factory.make_StaticIPAddress(
                ip="1.2.0.{}".format(n),
                alloc_type=IPADDRESS_TYPE.STICKY,
                subnet=subnet,
            )
        self.assertEqual(
            stats.get_subnets_utilisation_stats(),
            {
                "1.2.0.0/16": {
                    "available": 2 ** 16 - 36,
                    "dynamic_available": 9,
                    "dynamic_used": 1,
                    "reserved_available": 18,
                    "reserved_used": 2,
                    "static": 3,
                    "unavailable": 34,
                }
            },
        )


class TestStatsService(MAASTestCase):
    """Tests for `ImportStatsService`."""

    def test_is_a_TimerService(self):
        service = stats.StatsService()
        self.assertIsInstance(service, TimerService)

    def test_runs_once_a_day(self):
        service = stats.StatsService()
        self.assertEqual(86400, service.step)

    def test_calls__maybe_make_stats_request(self):
        service = stats.StatsService()
        self.assertEqual(
            (service.maybe_make_stats_request, (), {}), service.call
        )

    def test_maybe_make_stats_request_does_not_error(self):
        service = stats.StatsService()
        deferToDatabase = self.patch(stats, "deferToDatabase")
        exception_type = factory.make_exception_type()
        deferToDatabase.return_value = fail(exception_type())
        d = service.maybe_make_stats_request()
        self.assertIsNone(extract_result(d))


class TestStatsServiceAsync(MAASTransactionServerTestCase):
    """Tests for the async parts of `StatsService`."""

    def test_maybe_make_stats_request_makes_request(self):
        mock_call = self.patch(stats, "make_maas_user_agent_request")

        with transaction.atomic():
            Config.objects.set_config("enable_analytics", True)

        service = stats.StatsService()
        maybe_make_stats_request = asynchronous(
            service.maybe_make_stats_request
        )
        maybe_make_stats_request().wait(5)

        self.assertThat(mock_call, MockCalledOnce())

    def test_maybe_make_stats_request_doesnt_make_request(self):
        mock_call = self.patch(stats, "make_maas_user_agent_request")

        with transaction.atomic():
            Config.objects.set_config("enable_analytics", False)

        service = stats.StatsService()
        maybe_make_stats_request = asynchronous(
            service.maybe_make_stats_request
        )
        maybe_make_stats_request().wait(5)

        self.assertThat(mock_call, MockNotCalled())
