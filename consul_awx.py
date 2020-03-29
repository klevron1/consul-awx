#!/usr/bin/env python3
import argparse
import configparser
import copy
import json
import logging
import os
import sys
from urllib.parse import urlparse

import urllib3
from requests.exceptions import ConnectionError

try:
    import consul
except ImportError:
    sys.exit(
        """failed=True msg='python-consul2 required for this module.
See https://python-consul2.readthedocs.io/en/latest/'"""
    )

CONFIG = "consul_awx.ini"
DEFAULT_CONFIG_DIR = os.path.dirname(os.path.realpath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, CONFIG)

EMPTY_GROUP = {"hosts": [], "children": []}

EMPTY_INVENTORY = {
    "_meta": {"hostvars": {}},
    "all": {"hosts": [], "children": ["ungrouped"]},
    "ungrouped": copy.deepcopy(EMPTY_GROUP),
}


class ConsulInventory:
    def __init__(
        self, host="127.0.0.1", port=8500, token=None, scheme="http", verify=True
    ):

        if not str2bool(verify):
            verify = False
            # If the user disable SSL verification no need to bother him with
            # warning
            urllib3.disable_warnings()

        self.consul_api = consul.Consul(
            host=host, port=port, token=token, scheme=scheme, verify=verify
        )

        self.inventory = copy.deepcopy(EMPTY_INVENTORY)

    def build_full_inventory(self):
        for node in self.get_nodes():
            self.inventory["_meta"]["hostvars"][node["Node"]] = get_node_vars(node)
            meta = node.get("Meta", {})
            if meta is None:
                meta = {}
            for key, value in meta.items():

                if not value:
                    continue

                try:
                    value = str2bool(value.strip())
                except ValueError:
                    pass
                # Meta can only be string but we can pseudo support bool
                # We don't want groups named <osef>_false because by convention
                # this means the host is *not* in the group
                if value is False:
                    continue
                elif value is True:
                    group = key
                # Otherwise we want a group name by concatening key/value
                else:
                    group = f"{key}_{value}"

                self.add_to_group(group, node["Node"])

            # Build node services by using the service's name as group name
            services = self.get_node_services(node["Node"])
            for service, data in services.items():
                self.add_to_group(service, node["Node"])
                for tag in data["Tags"]:
                    self.add_to_group(f"{service}_{tag}", node["Node"])
                    # We want to define group nesting
                    if f"{service}_{tag}" not in self.inventory[service]["children"]:
                        self.inventory[service]["children"].append(f"{service}_{tag}")

        all_groups = [
            k for k in self.inventory.keys() if k not in ["_meta", "all", "ungrouped"]
        ]
        self.inventory["all"]["children"].extend(all_groups)
        # Better for humanreadable
        self.inventory["all"]["children"].sort()

    def add_to_group(self, group, host, parent=None):
        if group not in self.inventory:
            self.inventory[group] = copy.deepcopy(EMPTY_GROUP)
        self.inventory[group]["hosts"].append(host)

    def get_nodes(self, datacenter=None):
        logging.debug("getting all nodes for datacenter: %s", datacenter)
        return self.consul_api.catalog.nodes(dc=datacenter)[1]

    def get_node(self, node):
        logging.debug("getting node info for node: %s", node)
        return self.consul_api.catalog.node(node)[1]

    def get_node_services(self, node):
        logging.debug("getting services for node: %s", node)
        return self.get_node(node)["Services"]


def get_node_vars(node):
    node_vars = {"ansible_host": node["Address"], "datacenter": node["Datacenter"]}
    meta = node.get("Meta", {})
    if meta is None:
        meta = {}
    for k, v in meta.items():
        # Meta are all strings in consul
        if not v:
            continue
        v = v.strip()

        if v.isdigit():
            node_vars[k] = int(v)
        elif v.lower() == "true":
            node_vars[k] = True
        elif v.lower() == "false":
            node_vars[k] = False
        else:
            node_vars[k] = v

    return node_vars


def cmdline_parser():
    parser = argparse.ArgumentParser(
        description="Produce an Ansible Inventory file based nodes in a Consul cluster"
    )

    command_group = parser.add_mutually_exclusive_group(required=True)

    command_group.add_argument(
        "--list",
        action="store_true",
        dest="list",
        help="Get all inventory variables from all nodes in the consul cluster",
    )
    command_group.add_argument(
        "--host",
        action="store",
        dest="host",
        help="Get all inventory variables about a specific consul node,"
        "requires datacenter set in consul.ini.",
    )

    parser.add_argument(
        "--path", help="path to configuration file", default=DEFAULT_CONFIG_PATH
    )
    parser.add_argument(
        "--datacenter",
        action="store",
        help="Get all inventory about a specific consul datacenter",
    )

    parser.add_argument("--indent", type=int, default=4)
    parser.add_argument(
        "-d",
        "--debug",
        help="Print lots of debugging statements",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.WARNING,
    )  # mind the default value

    parser.add_argument(
        "-v",
        "--verbose",
        help="Be verbose",
        action="store_const",
        dest="loglevel",
        const=logging.INFO,
    )

    parser.add_argument(
        "-q",
        "--quiet",
        help="Be quiet",
        action="store_const",
        dest="loglevel",
        const=logging.CRITICAL,
    )

    args = parser.parse_args()
    logging.basicConfig(level=args.loglevel)
    return args


def str2bool(v):
    if isinstance(v, bool):
        return v
    elif v.lower() in ["true", "1", "yes"]:
        return True
    elif v.lower() in ["false", "0", "no"]:
        return False
    else:
        raise ValueError


def main():
    args = cmdline_parser()
    consul_config = {}

    if "CONSUL_URL" in os.environ:
        consul_url = os.environ["CONSUL_URL"]
        url = urlparse(consul_url)
        consul_config = {
            "host": url.hostname,
            "port": url.port,
            "scheme": url.scheme,
            "verify": str2bool(os.environ.get("CONSUL_SSL_VERIFY", True)),
            "token": os.environ.get("CONSUL_TOKEN"),
        }
    elif os.path.isfile(args.path):
        config = configparser.ConfigParser()
        config.read(args.path)
        if config.has_section("consul"):
            consul_config = dict(config.items("consul"))
    else:
        logging.debug("No envvar nor configuration file, will use default values")

    c = ConsulInventory(**consul_config)

    try:
        if args.host:
            result = get_host_var(c.get_node(args.host)["Node"])
        else:
            c.build_full_inventory()
            result = c.inventory
    except ConnectionError as err:
        logging.debug(str(err))
        logging.fatal("Failed to connect to consul")
        sys.exit(1)

    print(json.dumps(result, sort_keys=True, indent=args.indent))


if __name__ == "__main__":
    main()
