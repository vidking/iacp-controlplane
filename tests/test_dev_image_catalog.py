import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.models import InstanceSpec


ROOT = Path(__file__).parents[1]


@pytest.mark.no_database
def test_dev_catalog_declares_unique_fedora_and_debian_templates_without_production_entries():
    dev = json.loads(
        (ROOT / "terraform/environments/mng01/catalog-dev.json").read_text()
    )
    production = json.loads(
        (ROOT / "terraform/environments/mng01/catalog.json").read_text()
    )
    dev_images = {item["image_id"]: item for item in dev["images"]}
    production_ids = {item["image_id"] for item in production["images"]}

    assert len(dev_images) == len(dev["images"])
    assert "fedora-45" not in production_ids
    assert "debian-13" not in production_ids
    assert {
        image_id: (
            dev_images[image_id]["distribution"],
            dev_images[image_id]["version"],
            dev_images[image_id]["template_vm_id"],
        )
        for image_id in ("fedora-45", "debian-13")
    } == {
        "fedora-45": ("Fedora", "45", 29005),
        "debian-13": ("Debian", "13", 29006),
    }
    for image_id in ("fedora-45", "debian-13"):
        image = dev_images[image_id]
        assert image["source_node"] == "mng01"
        assert image["storage"] == "DS_CYBERFUEL"
        assert image["tag"] == image_id
        assert image["cloudinit_username"] == "root"
        assert image["minimum_vcpus"] == 1
        assert image["minimum_memory_mb"] == 2048
        assert image["minimum_disk_gb"] == 20


@pytest.mark.no_database
@pytest.mark.parametrize("image_id", ["fedora-45", "debian-13"])
def test_generic_instance_model_accepts_new_dev_image_ids(image_id):
    instance = InstanceSpec(name="new-image-pilot", image=image_id)
    assert instance.image == image_id


@pytest.mark.no_database
def test_generic_instance_model_rejects_undeclared_image_id():
    with pytest.raises(ValidationError):
        InstanceSpec(name="unknown-image", image="debian-12")
