"""
Strategy Tester helper for the AI EA (EA/AIEA_M5.mq5).

WebRequest is not available in the MT5 Strategy Tester, so backtesting the AI goes in 3 passes:
  1. Tester run with InpTesterMode = EXPORT  -> Common\\Files\\AIEA_requests.jsonl (one request per line)
  2. python ea_backtest.py <requests.jsonl> <replay.tsv>   (this script: asks the AI once per bar)
  3. Tester run with InpTesterMode = REPLAY  -> reads Common\\Files\\AIEA_replay.tsv, trades the answers

    python ea_backtest.py "%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files\\AIEA_requests.jsonl" ^
                          "%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files\\AIEA_replay.tsv" --max-cost 3 --model claude-haiku-5-5

Keys/provider come from the Dashboard settings (data/settings.json). The replay file is appended to, so a
stopped run can be resumed and bars already answered are never paid for twice. Backtest calls are not
written to the live AI EA stats and ignore the daily budget; --max-cost caps this run instead.
Note: the AI may remember historical gold prices from its training data, so backtest results can look
better than live results. Compare with InpTesterMode = RULES and with a demo forward test.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ea_ai   # noqa: E402
import store   # noqa: E402


def headers(key):
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    ws = (store.load("settings")["ai"].get("anthropic_workspace_id") or "").strip()
    if ws:
        h["anthropic-workspace-id"] = ws
    return h


def main():
    ap = argparse.ArgumentParser(description="Answer exported AI EA requests and write the replay file.")
    ap.add_argument("requests", help="AIEA_requests.jsonl written by the EA in EXPORT mode")
    ap.add_argument("replay", help="AIEA_replay.tsv read by the EA in REPLAY mode (appended)")
    ap.add_argument("--max-cost", type=float, default=2.0, help="stop after this many USD in this run")
    ap.add_argument("--model", default="", help="override the model (e.g. claude-haiku-5-5 for a cheap pass)")
    ap.add_argument("--effort", default="", help="override output_config.effort")
    ap.add_argument("--limit", type=int, default=0, help="answer at most N new bars (0 = all)")
    args = ap.parse_args()

    done = set()
    if os.path.exists(args.replay):
        with open(args.replay, encoding="utf-8") as f:
            for line in f:
                done.add(line.split("\t", 1)[0])

    reqs, seen = [], set()
    with open(args.requests, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            key = str(r.get("bar_time") or "")
            if key and key not in seen and key not in done:
                seen.add(key)
                reqs.append((key, r))
    print(f"{len(reqs)} new bars to answer ({len(done)} already in the replay file)")

    ov = {"no_budget": True, "no_log": True}
    if args.model:
        ov["model"] = args.model
    if args.effort:
        ov["effort"] = args.effort
    ai = store.load("settings")["ai"]
    cost = calls = errors = 0.0
    with open(args.replay, "a", encoding="utf-8") as out:
        for i, (key, r) in enumerate(reqs, 1):
            if args.limit and i > args.limit:
                break
            res = ea_ai.decide(r, ai, headers, ov)
            calls += 1
            cost += float(res.get("cost_usd") or 0)
            if res.get("ok"):
                out.write(key + "\t" + json.dumps(res, ensure_ascii=False, separators=(",", ":")) + "\n")
                out.flush()
                print(f"[{i}/{len(reqs)}] {key}  {res['decision']:4s} buy={res['buy_score']} sell={res['sell_score']} "
                      f"cost={res['cost_usd']:.4f} total={cost:.3f}  {res.get('reject') or res.get('setup')}")
            else:
                errors += 1
                print(f"[{i}/{len(reqs)}] {key}  ERROR {res.get('error')}")
                if "API Key" in str(res.get("error")) or errors >= 5:
                    print("stopping: too many errors")
                    break
            if cost >= args.max_cost:
                print(f"stopping: --max-cost {args.max_cost} USD reached (run again to continue)")
                break
    print(f"done: {int(calls)} calls, {cost:.3f} USD, {int(errors)} errors -> {args.replay}")


if __name__ == "__main__":
    main()
