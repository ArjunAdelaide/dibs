"""Terminal simulator of an iMessage thread. Same agent, no phone needed.

    python -m dibs.cli                  # you are +61400000001
    python -m dibs.cli --as +61400000002 --chat group-test --group
"""

import argparse

from . import db
from .agent import run_turn
from .catalog import Catalog
from .llm import OpenAICompatLLM


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as", dest="handle", default="+61400000001")
    parser.add_argument("--chat", default=None, help="conversation id; defaults to the handle")
    parser.add_argument("--group", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    catalog = Catalog.load()
    llm = OpenAICompatLLM()
    conv_id = args.chat or f"cli;-;{args.handle}"
    print(f"Texting Dibs as {args.handle} ({len(catalog.venues)} venues, {len(catalog.deals)} deals). Ctrl-D to quit.")
    while True:
        try:
            text = input("you> ").strip()
        except EOFError:
            break
        if text:
            print("dibs>", run_turn(conn, catalog, llm, conv_id, args.handle, text,
                                      lambda m: print(f"  [operator alert] {m}"), is_group=args.group,
                                      send_later=lambda later: print(f"\ndibs (later)> {later}\nyou> ", end="", flush=True)))


if __name__ == "__main__":
    main()
