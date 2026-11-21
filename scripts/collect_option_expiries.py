"""归档交易所历史逐腿参考资料，复用已核验原件。"""
from argparse import ArgumentParser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import gzip
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

import requests
from app.agent_replay import BJT
from scripts.collect_replay_market import atomic
from scripts.agent_replay import atomic as save_json
from scripts.expiry_settlement import ABANDON_HASH, ABANDON_URL


def archive_rules(out):
    documents = {
        ABANDON_HASH: ABANDON_URL,
        "cbf2876ea437dce14f949de1c5541b8e6379c7320b4e64f4aef352438f8dd0a1":
            "https://www.czce.com.cn/cn/rootfiles/2024/03/26/1708572241871702-1708572241883510.pdf",
    }
    for digest, url in documents.items():
        path = out / "expiry-rules" / (digest + ".pdf")
        if path.is_file():
            data = path.read_bytes()
        else:
            response = requests.get(url, timeout=(6,25), allow_redirects=False)
            response.raise_for_status()
            data = response.content
        if sha256(data).hexdigest() != digest:
            raise ValueError("交易所规则文件与核证版本不一致")
        if not path.is_file():
            atomic(path, data)
    save_json(out / "expiry-rule-manifest.json", documents)


def collect_snapshot(root, day, exchange, universe):
    digits = day.replace("-", "")
    cz = exchange == "CZCE"
    url = (f"https://www.czce.com.cn/cn/DFSStaticFiles/Option/{digits[:4]}/{digits}/OptionDataReferenceData.xml" if cz
           else f"https://www.shfe.com.cn/data/busiparamdata/option/ContractBaseInfo{digits}.dat")
    receipt_path = root / f"{exchange}-{day}.json"
    if receipt_path.is_file():
        rec = json.loads(receipt_path.read_bytes())
        raw = gzip.decompress((root / rec["raw"]).read_bytes())
        if sha256(raw).hexdigest() != rec["sha256"] or rec["url"] != url:
            raise ValueError("到期原件摘要或地址不符")
    else:
        response = requests.get(url, timeout=(6, 25), allow_redirects=False)
        response.raise_for_status()
        raw = response.content
        digest = sha256(raw).hexdigest()
        modified = response.headers.get("Last-Modified")
        if not modified or response.status_code != 200:
            raise ValueError("原件缺少版本时点")
        rec = {"day": day, "exchange": exchange, "url": url, "sha256": digest, "raw": f"{digest}.gz",
               "retrieved_at": datetime.now(timezone.utc).isoformat(),
               "available_at": max(parsedate_to_datetime(modified), datetime.fromisoformat(day).replace(hour=15,tzinfo=BJT)).astimezone(BJT).isoformat()}
    if cz:
        xml = ET.fromstring(raw)
        rows = ((x.findtext("CtrCd"), x.findtext("ExpiryDt")) for x in xml
                if x.findtext("PrdTp") == "常规期权" and x.findtext("LstTrdDt") == x.findtext("ExpiryDt"))
    else:
        data = json.loads(raw)
        if data["report_date"] != digits:
            raise ValueError("上期所公告日与请求不一致")
        rows = ((r["INSTRUMENTID"], datetime.strptime(r["EXPIREDATE"], "%Y%m%d").date().isoformat())
                for r in data["OptionContractBaseInfo"] if r["TRADINGDAY"] == digits)
    grouped = defaultdict(list)
    for ident, exp in rows:
        m = re.fullmatch(r"([A-Z]+)(\d{3,4})([CP]\d+(?:\.\d+)?)", ident.strip().upper())
        if not m or m[1] not in universe or not exp:
            continue
        month = m[2]
        if len(month) == 3:
            month = digits[2] + month
        grouped[exp].append(m[1]+month+m[3])
    if not grouped:
        raise ValueError("未取得可识别的常规合约")
    if not receipt_path.is_file():
        atomic(root/rec["raw"], gzip.compress(raw, 3, mtime=0))
        save_json(receipt_path, rec)
    return {**rec, "series": [{"expiry": k, "legs": sorted(v)} for k,v in sorted(grouped.items())]}


def run(study, out):
    archive_rules(out)
    manifest = json.loads((study/"market/source-manifest.json").read_bytes())
    calendar = manifest["calendar"]
    days = {d for i,d in enumerate(calendar) if i==0 or calendar[i-1][:7]!=d[:7]}
    days.update(["2026-05-20", "2026-08-27"])
    # 加入所有配对事件的决策日，以核验当时新挂牌的行权价。
    from scripts.term_study import event_slots, timings
    window = json.loads((study/"run-manifest.json").read_bytes())["calendar"]
    for event in event_slots(study, window):
        days.add(timings(event, calendar)[0])
    root = out/"expiry-originals"
    root.mkdir(parents=True,exist_ok=True)
    jobs = [(d,x) for d in sorted(days) for x in ("CZCE","SHFE")]
    def fetch(job):
        return collect_snapshot(root, *job, manifest["configured_universe"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(fetch,jobs))
    save_json(out/"expiry-index.json", {"sources": snapshots})
    print(len(snapshots), "份逐腿原件", sum(len(g["legs"]) for d in snapshots for g in d["series"]), "条参数记录")


if __name__ == "__main__":
    parser=ArgumentParser(description=__doc__);parser.add_argument("--study",type=Path,required=True);parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args();run(args.study,args.out)
