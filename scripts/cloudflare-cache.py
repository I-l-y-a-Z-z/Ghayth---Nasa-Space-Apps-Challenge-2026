"""Seed a fresh R2 namespace or restore a scientific checkpoint; credentials stay in env."""

import argparse

from agrix.earth.object_store import cache_store_from_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seed", "restore"))
    parser.add_argument("--directory", default="data/cache")
    args = parser.parse_args()
    store = cache_store_from_env(args.directory)
    if store is None:
        parser.error(
            "Set AGRIX_R2_BUCKET, endpoint and scoped credentials in your environment"
        )
    if args.operation == "seed":
        if store._index():
            parser.error(
                "Refusing to overwrite an existing R2 checkpoint; use a fresh prefix"
            )
        count = store.checkpoint()
    else:
        count = store.restore()
    print(
        f"{args.operation}: {count} scientific files; no farm records or model weights transferred"
    )


if __name__ == "__main__":
    main()
