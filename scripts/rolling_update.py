"""运行一次滚动模拟账户的盘前封存或日结。"""

import json
from argparse import ArgumentParser
from pathlib import Path

from app.july_daily import tick

if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--cli")
    parser.add_argument("--model", default="gpt-6.2-sol")
    args = parser.parse_args()
    print(
        json.dumps(
            tick(args.workspace, model=args.model, cli=args.cli), ensure_ascii=False
        )
    )
