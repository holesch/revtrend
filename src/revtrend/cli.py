import argparse


def main():
    parser = argparse.ArgumentParser(prog="revtrend")
    parser.add_subparsers(dest="command", required=True)
    parser.parse_args()
