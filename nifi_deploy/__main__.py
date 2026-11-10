import argparse
import json
import os
import sys

from .api import Client
from .bundle import read_json, validate_bundle
from .configuration import export_deployment
from .docker_export import capture_docker
from .portable_deployment import deploy_config


def main():
    parser = argparse.ArgumentParser(description="NiFi Deploy Kit: native flow export and Docker Compose deployment")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--url", default=os.environ.get("NIFI_SOURCE_URL"))
    export.add_argument("--name", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--group-id")
    export.add_argument("--auth-file", help="Private JSON containing username/password")
    export.add_argument("--ca-file")
    source = export.add_mutually_exclusive_group(required=True)
    source.add_argument("--docker-host", help="SSH user@host for read-only Docker capture")
    source.add_argument("--docker-local", action="store_true", help="Read Docker on this host")
    export.add_argument("--docker-sudo", action="store_true", help="Use sudo for Docker reads on source (passwordless or private env password)")
    export.add_argument("--compose-file", required=True, help="Absolute source Compose file")
    export.add_argument("--compose-project", default="")
    export.add_argument("--ssh-port", type=int, default=22)
    export.add_argument("--ssh-identity")
    export.add_argument("--ssh-known-hosts")
    export.add_argument("--nifi-service", default="nifi")
    export.add_argument("--gateway-service", default="nginx")
    validate = commands.add_parser("validate")
    validate.add_argument("bundles", nargs="+")
    install = commands.add_parser("deploy")
    install.add_argument("--config", required=True, help="Customer-edited config.json")
    install.add_argument("--overwrite-config", action="store_true")
    install.add_argument("--start", action="store_true")
    install.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        if args.command == "export":
            if not args.url:
                raise ValueError("--url or NIFI_SOURCE_URL is required")
            auth = read_json(args.auth_file) if args.auth_file else {
                "username": os.environ.get("NIFI_SOURCE_USERNAME", ""),
                "password": os.environ.get("NIFI_SOURCE_PASSWORD", "")}
            client = Client(args.url, auth.get("username", ""), auth.get("password", ""), args.ca_file)
            raw = capture_docker(args.docker_host, args.compose_file, args.compose_project,
                                 args.ssh_port, args.ssh_identity, args.ssh_known_hosts, args.docker_sudo)
            config = export_deployment(client, raw, args.output, args.name, args.group_id, args.nifi_service, args.gateway_service)
            result = {"name": config["name"], "schemaVersion": 2, "counts": config["flow"]["counts"],
                      "services": list(config["docker"]["compose"]["services"]), "configFile": args.output + "/config.json"}
        elif args.command == "validate":
            result = [{"name": (r := validate_bundle(p))["name"], "counts": r["counts"], "valid": True}
                      for p in args.bundles]
        else:
            result = deploy_config(args.config, args.overwrite_config, args.start, args.timeout)
            result = {**{k: v for k, v in result.items() if k not in ("invalidProcessors", "typeMismatches")},
                      "invalidProcessorCount": len(result["invalidProcessors"]),
                      "typeMismatchCount": len(result["typeMismatches"])}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
