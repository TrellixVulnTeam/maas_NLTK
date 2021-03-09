# Generated by Django 2.2.12 on 2021-02-15 11:21

from itertools import chain

from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone

import maasserver.models.cleansave


def sync_vm_disks(apps, schema_editor):
    PhysicalBlockDevice = apps.get_model("maasserver", "PhysicalBlockDevice")
    ISCSIBlockDevice = apps.get_model("maasserver", "ISCSIBlockDevice")
    VirtualMachineDisk = apps.get_model("maasserver", "VirtualMachineDisk")

    now = timezone.now()
    physical_disks_info = PhysicalBlockDevice.objects.exclude(
        node__virtualmachine=None
    ).values(
        "name",
        "size",
        block_device_id=models.F("id"),
        vm_id=models.F("node__virtualmachine__id"),
        backing_pool_id=models.F("storage_pool_id"),
    )
    iscsi_disks_info = ISCSIBlockDevice.objects.exclude(
        node__virtualmachine=None
    ).values(
        "name",
        "size",
        block_device_id=models.F("id"),
        vm_id=models.F("node__virtualmachine__id"),
    )
    VirtualMachineDisk.objects.bulk_create(
        VirtualMachineDisk(**info, created=now, updated=now)
        for info in chain(physical_disks_info, iscsi_disks_info)
    )


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0223_virtualmachine_blank_project"),
    ]

    operations = [
        migrations.CreateModel(
            name="VirtualMachineDisk",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(editable=False)),
                ("updated", models.DateTimeField(editable=False)),
                ("name", models.CharField(max_length=255)),
                ("size", models.BigIntegerField()),
                (
                    "backing_pool",
                    models.ForeignKey(
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vmdisks_set",
                        to="maasserver.PodStoragePool",
                    ),
                ),
                (
                    "block_device",
                    models.OneToOneField(
                        blank=True,
                        default=None,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vmdisk",
                        to="maasserver.BlockDevice",
                    ),
                ),
                (
                    "vm",
                    models.ForeignKey(
                        editable=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="disks_set",
                        to="maasserver.VirtualMachine",
                    ),
                ),
            ],
            options={
                "unique_together": {("vm", "name")},
            },
            bases=(
                maasserver.models.cleansave.CleanSave,
                models.Model,
                object,
            ),
        ),
        migrations.RunPython(sync_vm_disks),
    ]