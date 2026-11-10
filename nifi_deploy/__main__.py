import argparse
import json
import os
import sys

from .api import Client
from .bundle import export_bundle, read_json, validate_bundle
from .deployment import deploy


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
    export.add_argument("--with-registry", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("bundles", nargs="+")
    install = commands.add_parser("deploy")
    install.add_argument("--bundle", required=True)
    install.add_argument("--env", required=True)
    install.add_argument("--parameters")
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
            result = export_bundle(client, args.output, args.name, args.group_id, args.with_registry)
        elif args.command == "validate":
            result = [{"name": (r := validate_bundle(p))["name"], "counts": r["counts"], "valid": True}
                      for p in args.bundles]
        else:
            result = deploy(args.bundle, args.env, args.parameters, args.overwrite_config, args.start, args.timeout)
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
