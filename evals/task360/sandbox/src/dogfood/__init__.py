"""dogfood — a tiny CLI used as a test sandbox."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="dogfood")
    parser.parse_args()
    print("dogfood")
