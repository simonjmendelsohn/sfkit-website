import ipaddr
import os
import time

import googleapiclient.discovery as googleapi
from src.utils import constants
from tenacity import retry
from tenacity.stop import stop_after_attempt
from tenacity.wait import wait_fixed


class GoogleCloudCompute:
    def __init__(self, project) -> None:
        self.project = project
        self.compute = googleapi.build("compute", "v1")

    def setup_networking(self, doc_ref_dict: dict, role: str) -> None:
        gcp_projects = [constants.SERVER_GCP_PROJECT]
        gcp_projects.extend(
            doc_ref_dict["personal_parameters"][participant]["GCP_PROJECT"]["value"]
            for participant in doc_ref_dict["participants"]
        )

        self.create_network()
        self.remove_conflicting_peerings(gcp_projects)
        self.remove_conflicting_subnets(gcp_projects)
        self.create_subnet(role)
        self.create_peerings(gcp_projects)

    def create_network(self, network_name: str = constants.NETWORK_NAME) -> None:
        networks = self.compute.networks().list(project=self.project).execute()["items"]
        network_names = [net["name"] for net in networks]

        if network_name not in network_names:
            print(f"Creating new network {network_name}")
            req_body = {
                "name": network_name,
                "autoCreateSubnetworks": False,
                "routingConfig": {"routingMode": "GLOBAL"},
            }
            operation = (
                self.compute.networks()
                .insert(project=self.project, body=req_body)
                .execute()
            )
            self.wait_for_operation(operation["name"])

            self.create_firewall(network_name)

    def create_firewall(self, network_name: str = constants.NETWORK_NAME) -> None:
        print(f"Creating new firewalls for network {network_name}")
        network_url = ""
        for net in (
            self.compute.networks().list(project=self.project).execute()["items"]
        ):
            if net["name"] == network_name:
                network_url = net["selfLink"]

        firewall_body = {
            "name": f"{network_name}-vm-ingress",
            "network": network_url,
            "sourceRanges": ["0.0.0.0/0"],
            "allowed": [{"ports": ["8000-8999", "22"], "IPProtocol": "tcp"}],
        }

        operation = (
            self.compute.firewalls()
            .insert(project=self.project, body=firewall_body)
            .execute()
        )
        self.wait_for_operation(operation["name"])

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(10))
    def remove_conflicting_peerings(self, gcp_projects: list) -> None:
        # a peering is conflicting if it connects to a project that is not in the current study
        network_info = (
            self.compute.networks()
            .get(project=self.project, network=constants.NETWORK_NAME)
            .execute()
        )
        peerings = [
            peer["name"].replace("peering-", "")
            for peer in network_info.get("peerings", [])
        ]

        for other_project in peerings:
            if other_project not in gcp_projects:
                print(f"Deleting peering from {self.project} to {other_project}")
                body = {"name": f"peering-{other_project}"}
                self.compute.networks().removePeering(
                    project=self.project, network=constants.NETWORK_NAME, body=body
                ).execute()
                time.sleep(2)

    def remove_conflicting_subnets(self, gcp_projects: list) -> None:
        # a subnet is conflicting if it has an IpCidrRange that does not match the expected ranges based on the roles of participants in the study
        subnets = (
            self.compute.subnetworks()
            .list(project=self.project, region=constants.REGION)
            .execute()["items"]
        )
        ip_cidr_ranges_for_this_network = [
            f"10.0.{i}.0/24" for i in range(3) if gcp_projects[i] == self.project
        ]
        for subnet in subnets:
            if (
                constants.NETWORK_NAME in subnet["network"]
                and subnet["ipCidrRange"] not in ip_cidr_ranges_for_this_network
            ):
                n1 = ipaddr.IPNetwork(subnet["ipCidrRange"])
                if any(
                    n1.overlaps(ipaddr.IPNetwork(n2))
                    for n2 in ip_cidr_ranges_for_this_network
                ):
                    self.delete_subnet(subnet)

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(30))
    def delete_subnet(self, subnet: dict) -> None:
        for instance in self.list_instances(
            constants.ZONE, subnetwork=subnet["selfLink"]
        ):
            self.delete_instance(instance)

        print(f"Deleting subnet {subnet['name']}")
        self.compute.subnetworks().delete(
            project=self.project,
            region=constants.REGION,
            subnetwork=subnet["name"],
        ).execute()

        # wait for the subnet to be deleted
        for i in range(30):
            if i == 25:
                print(f"Failure to delete subnet {subnet['name']}")
                quit(1)
            subnets = (
                self.compute.subnetworks()
                .list(project=self.project, region=constants.REGION)
                .execute()["items"]
            )
            if subnet["name"] not in [sub["name"] for sub in subnets]:
                break
            time.sleep(2)

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(30))
    def create_subnet(self, role: str, region: str = constants.REGION) -> None:
        # create subnet if it doesn't already exist
        subnet_name = constants.SUBNET_NAME + role
        subnets = (
            self.compute.subnetworks()
            .list(project=self.project, region=constants.REGION)
            .execute()["items"]
        )
        subnet_names = [subnet["name"] for subnet in subnets]
        if subnet_name not in subnet_names:
            print(f"Creating new subnetwork {subnet_name}")
            network_url = ""
            for net in (
                self.compute.networks().list(project=self.project).execute()["items"]
            ):
                if net["name"] == constants.NETWORK_NAME:
                    network_url = net["selfLink"]

            req_body = {
                "name": subnet_name,
                "network": network_url,
                "ipCidrRange": f"10.0.{role}.0/28",
            }
            operation = (
                self.compute.subnetworks()
                .insert(project=self.project, region=region, body=req_body)
                .execute()
            )
            self.wait_for_regionOperation(region, operation["name"])

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(30))
    def create_peerings(self, gcp_projects: list) -> None:
        # create peerings if they don't already exist
        network_info = (
            self.compute.networks()
            .get(project=self.project, network=constants.NETWORK_NAME)
            .execute()
        )
        peerings = [
            peer["name"].replace("peering-", "")
            for peer in network_info.get("peerings", [])
        ]
        other_projects = [p for p in gcp_projects if p != self.project]
        for other_project in other_projects:
            if other_project not in peerings:
                print("Creating peering from", self.project, "to", other_project)
                body = {
                    "networkPeering": {
                        "name": "peering-{}".format(other_project),
                        "network": "https://www.googleapis.com/compute/v1/projects/{}/global/networks/{}".format(
                            other_project, constants.NETWORK_NAME
                        ),
                        "exchangeSubnetRoutes": True,
                    }
                }
                self.compute.networks().addPeering(
                    project=self.project, network=constants.NETWORK_NAME, body=body
                ).execute()

    def setup_instance(
        self,
        zone,
        name,
        role,
        num_cpus=4,
        validate=False,
        metadata=None,
        boot_disk_size=10,
    ):
        existing_instances = self.list_instances(constants.ZONE)

        if existing_instances and name in existing_instances:
            self.delete_instance(name, zone)
        self.create_instance(
            zone,
            name,
            role,
            num_cpus,
            validate=validate,
            metadata=metadata,
            boot_disk_size=boot_disk_size,
        )

        return self.get_vm_external_ip_address(zone, name)

    def create_instance(
        self,
        zone,
        name,
        role,
        num_cpus,
        validate=False,
        metadata=None,
        boot_disk_size=10,
    ):
        print("Creating VM instance with name", name)

        image_response = (
            self.compute.images()
            .getFromFamily(project="ubuntu-os-cloud", family="ubuntu-2110")
            .execute()
        )
        source_disk_image = image_response["selfLink"]

        # Configure the machine
        machine_type = f"zones/{zone}/machineTypes/e2-highmem-{num_cpus}"
        if validate:
            startup_script = open(
                os.path.join(
                    os.path.dirname(__file__), "../../startup-script-validate.sh"
                ),
                "r",
            ).read()
        else:
            startup_script = open(
                os.path.join(os.path.dirname(__file__), "../../startup-script.sh"), "r"
            ).read()

        metadata_config = {
            "items": [
                {"key": "startup-script", "value": startup_script},
                {"key": "enable-oslogin", "value": True},
            ]
        }
        if metadata:
            metadata_config["items"].append(metadata)
            print("Metadata:", metadata_config)

        instance_body = {
            "name": name,
            "machineType": machine_type,
            "networkInterfaces": [
                {
                    "network": "projects/{}/global/networks/{}".format(
                        self.project, constants.NETWORK_NAME
                    ),
                    "subnetwork": "regions/{}/subnetworks/{}".format(
                        constants.REGION, constants.SUBNET_NAME + role
                    ),
                    "networkIP": f"10.0.{role}.10",
                    "accessConfigs": [
                        {
                            "type": "ONE_TO_ONE_NAT",
                            "name": "External NAT",
                        }  # This is necessary to give the VM access to the internet, which it needs to do things like download the git repos.
                        # See (https://cloud.google.com/compute/docs/reference/rest/v1/instances) for more information.  If it helps, the external IP address is ephemeral.
                    ],
                }
            ],
            "disks": [
                {
                    "boot": True,
                    "autoDelete": True,
                    "initializeParams": {"sourceImage": source_disk_image},
                    "diskSizeGb": boot_disk_size,
                }
            ],
            # Allow the instance to access cloud storage and logging.
            # Logging access necessary for the startup-script to work.
            "serviceAccounts": [
                {
                    "email": "default",
                    "scopes": [
                        "https://www.googleapis.com/auth/devstorage.read_write",
                        "https://www.googleapis.com/auth/logging.write",
                        "https://www.googleapis.com/auth/pubsub",
                    ],
                }
            ],
            "metadata": metadata_config,
        }
        operation = (
            self.compute.instances()
            .insert(project=self.project, zone=zone, body=instance_body)
            .execute()
        )

        self.wait_for_zoneOperation(zone, operation["name"])

    def stop_instance(self, zone: str, instance: str) -> None:
        print("Stopping VM instance with name ", instance)
        operation = (
            self.compute.instances()
            .stop(project=self.project, zone=zone, instance=instance)
            .execute()
        )
        self.wait_for_zoneOperation(zone, operation["name"])

    def list_instances(self, zone=constants.ZONE, subnetwork=""):
        result = (
            self.compute.instances().list(project=self.project, zone=zone).execute()
        )
        return (
            [
                instance["name"]
                for instance in result["items"]
                if subnetwork in instance["networkInterfaces"][0]["subnetwork"]
            ]
            if "items" in result
            else []
        )

    def delete_instance(self, name, zone=constants.ZONE):
        print("Deleting VM instance with name ", name)
        operation = (
            self.compute.instances()
            .delete(project=self.project, zone=zone, instance=name)
            .execute()
        )
        self.wait_for_zoneOperation(zone, operation["name"])

    def wait_for_operation(self, operation):
        print("Waiting for operation to finish...")
        while True:
            result = (
                self.compute.globalOperations()
                .get(project=self.project, operation=operation)
                .execute()
            )

            if result["status"] == "DONE":
                print("done.")
                if "error" in result:
                    raise Exception(result["error"])
                return result

            time.sleep(1)

    def wait_for_zoneOperation(self, zone, operation):
        print("Waiting for operation to finish...")
        while True:
            result = (
                self.compute.zoneOperations()
                .get(project=self.project, zone=zone, operation=operation)
                .execute()
            )

            if result["status"] == "DONE":
                print("done.")
                if "error" in result:
                    raise Exception(result["error"])
                return result

            time.sleep(1)

    def wait_for_regionOperation(self, region, operation):
        print("Waiting for operation to finish...")
        while True:
            result = (
                self.compute.regionOperations()
                .get(project=self.project, region=region, operation=operation)
                .execute()
            )

            if result["status"] == "DONE":
                print("done.")
                if "error" in result:
                    raise Exception(result["error"])
                return result

            time.sleep(1)

    def get_vm_external_ip_address(self, zone, instance):
        print("Getting the IP address for VM instance", instance)
        response = (
            self.compute.instances()
            .get(project=self.project, zone=zone, instance=instance)
            .execute()
        )
        return response["networkInterfaces"][0]["accessConfigs"][0]["natIP"]

    def get_service_account_for_vm(self, zone, instance) -> str:
        print("Getting the service account for VM instance", instance)
        response = (
            self.compute.instances()
            .get(project=self.project, zone=zone, instance=instance)
            .execute()
        )
        return response["serviceAccounts"][0]["email"]
