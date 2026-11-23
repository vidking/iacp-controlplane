import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.models import InstanceSpec


ROOT = Path(__file__).parents[1]


@pytest.mark.no_database
def test_dev_y_produccion_publican_metadatos_humanos_para_las_seis_imagenes():
    expected = {
        "ubuntu-2404": (
            "Ubuntu", "24.04", "Ubuntu 24.04 LTS Noble Numbat",
            "Noble Numbat", "LTS", "debian",
        ),
        "ubuntu-2604": (
            "Ubuntu", "26.04", "Ubuntu 26.04 LTS Resolute Raccoon",
            "Resolute Raccoon", "LTS", "debian",
        ),
        "almalinux-9": (
            "AlmaLinux OS", "9", "AlmaLinux OS 9", None,
            "enterprise-linux", "rhel",
        ),
        "almalinux-10": (
            "AlmaLinux OS", "10", "AlmaLinux OS 10", None,
            "enterprise-linux", "rhel",
        ),
        "fedora-45": (
            "Fedora", "45", "Fedora Linux 45", None,
            "standard-release", "fedora",
        ),
        "debian-13": (
            "Debian", "13", "Debian 13 Trixie", "Trixie",
            "stable", "debian",
        ),
    }
    for filename in ("catalog-dev.json", "catalog.json"):
        catalog = json.loads(
            (ROOT / "terraform/environments/mng01" / filename).read_text()
        )
        images = {item["image_id"]: item for item in catalog["images"]}
        assert set(images) == set(expected)
        for image_id, metadata in expected.items():
            image = images[image_id]
            assert (
                image["distribution"],
                image["version"],
                image["display_name"],
                image["release_name"],
                image["release_track"],
                image["platform_family"],
            ) == metadata


@pytest.mark.no_database
def test_dev_and_production_catalogs_publish_the_same_fedora_and_debian_templates():
    dev = json.loads(
        (ROOT / "terraform/environments/mng01/catalog-dev.json").read_text()
    )
    production = json.loads(
        (ROOT / "terraform/environments/mng01/catalog.json").read_text()
    )
    dev_images = {item["image_id"]: item for item in dev["images"]}
    production_images = {item["image_id"]: item for item in production["images"]}

    assert len(dev_images) == len(dev["images"])
    assert len(production_images) == len(production["images"])
    assert dev["plans"] == production["plans"]
    assert dev_images == production_images
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
        image = production_images[image_id]
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
