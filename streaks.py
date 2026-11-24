"""Group a sanitized local result feed; exclude singleton streaks from model input."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import time

UTC = dt.timezone.utc
RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}


def stamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Result timestamp must include a timezone")
    return result.astimezone(UTC)


class StreakRecorder:
    def __init__(self, folder, timeout=60):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        if any(self.folder.iterdir()):
            raise ValueError("Choose a fresh streak output directory")
        (self.folder / ".gitignore").write_text("*\n", encoding="utf-8")
        self.timeout = timeout
        self.streaks = []
        self.active = None
        self.pending_first = None

    def append(self, path, row):
        with (self.folder / path).open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, allow_nan=False) + "\n")

    def publish(self):
        value = {"updated_utc": dt.datetime.now(UTC).isoformat(), "timeout_seconds": self.timeout,
                 "streaks": self.streaks, "total_streaks": len(self.streaks),
                 "eligible_streaks": sum(s["length"] >= 2 for s in self.streaks),
                 "excluded_singletons": sum(s["length"] == 1 and s["closed"] for s in self.streaks)}
        temporary = self.folder / "status.json.tmp"
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(self.folder / "status.json")
                return True
            except PermissionError:
                # Windows readers may briefly hold the old status file open.
                # Result journals remain authoritative; retry status next poll.
                time.sleep(0.02)
        return False

    def close(self, reason, at):
        if self.active is None:
            return
        current = self.active
        current.update(closed=True, closed_utc=at.isoformat(), close_reason=reason)
        if current["length"] >= 2:
            self.append("feed.jsonl", {"kind": "journal_gap"})
        self.active = None
        self.pending_first = None
        self.publish()

    def expire(self, now):
        if self.active:
            deadline = stamp(self.active["last_result_utc"]) + dt.timedelta(seconds=self.timeout)
            if now >= deadline:
                self.close("no_result_timeout", deadline)

    def accept(self, row):
        if row.get("kind") == "journal_gap":
            if self.active:
                self.close("source_gap", stamp(self.active["last_result_utc"]))
            return
        if row.get("kind") not in ("unverified_candidate", "verified_final_result"):
            raise ValueError("Streak input must be a sanitized accepted-result feed")
        number = row.get("number")
        if type(number) is not int or not 0 <= number <= 36:
            raise ValueError("Invalid result number")
        observed = stamp(row["receivedUtc"])
        if self.active:
            previous = stamp(self.active["last_result_utc"])
            if observed < previous:
                raise ValueError("Result timestamps are out of order")
            self.expire(observed)
        if self.active and row["tableId"] != self.active["source_id"]:
            self.close("source_changed", observed)
        if self.active is None:
            self.active = {"id": len(self.streaks) + 1, "source_id": row["tableId"],
                           "started_utc": observed.isoformat(), "last_result_utc": observed.isoformat(),
                           "length": 0, "closed": False, "model_eligible": False}
            self.streaks.append(self.active)
        current = self.active
        current["length"] += 1
        current["last_result_utc"] = observed.isoformat()
        color = "green" if number == 0 else "red" if number in RED else "black"
        identifier = current["id"]
        self.append(f"streak_{identifier:05d}.jsonl", {**row, "color": color})
        if current["length"] == 1:
            self.pending_first = dict(row)
        else:
            if current["length"] == 2:
                current["model_eligible"] = True
                self.append("feed.jsonl", {"kind": "journal_gap"})
                self.append("feed.jsonl", self.pending_first)
                self.pending_first = None
            self.append("feed.jsonl", row)
            # These files contain eligible sequences only; singletons have no txt file.
            with (self.folder / f"streak_{identifier:05d}.txt").open("a", encoding="utf-8") as output:
                if current["length"] == 2:
                    first = json.loads((self.folder / f"streak_{identifier:05d}.jsonl").read_text(encoding="utf-8").splitlines()[0])
                    output.write(f"{first['number']}\n")
                output.write(f"{number}\n")
        self.publish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    recorder = StreakRecorder(args.output, args.timeout)
    recorder.publish()
    # This input is the append-only, sanitized collector feed, not a rotated raw journal.
    with args.events.open("r", encoding="utf-8") as source:
        partial = ""
        try:
            while not (args.stop_file and args.stop_file.exists()):
                chunk = source.read(65536)
                if chunk:
                    partial += chunk
                    while "\n" in partial:
                        line, partial = partial.split("\n", 1)
                        if line.strip():
                            recorder.accept(json.loads(line))
                    continue
                recorder.expire(dt.datetime.now(UTC))
                recorder.publish()
                time.sleep(0.1)
        finally:
            recorder.close("watcher_stopped", dt.datetime.now(UTC))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
