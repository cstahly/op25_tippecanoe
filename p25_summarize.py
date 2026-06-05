#!/usr/bin/env python3
"""
Press Enter to get a Claude summary of P25 traffic since the last summary.
Run in a third terminal alongside OP25 and tail -f p25_log.txt.

    python3 ~/op25_tippecanoe/p25_summarize.py
"""

import os
import anthropic
from datetime import datetime

LOG_FILE = os.path.expanduser("~/op25_tippecanoe/p25_log.txt")
SUMMARY_MARKER = "=== SUMMARY ==="


def read_since_last_summary():
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    # Find the last summary marker, return everything after it
    last = -1
    for i, line in enumerate(lines):
        if SUMMARY_MARKER in line:
            last = i
    return [l.rstrip() for l in lines[last + 1:] if l.strip()]


def get_note():
    print("┌─ Note (optional) " + "─" * 40 + "┐")
    note = input("│ ").strip()
    print("└" + "─" * 58 + "┘")
    return note


def request_summary(lines, note=""):
    if not lines:
        print("No new traffic since last summary.")
        return

    block = "\n".join(lines)
    note_section = f"\nOperator note: {note}\n" if note else ""
    prompt = f"""You are an experienced public safety dispatcher reviewing radio traffic logs from Tippecanoe County, Indiana (Lafayette area). You understand police, fire, and EMS radio procedures and terminology fluently.{note_section}

Talkgroups:
- TEAS EMS DISPATCH (1833) / TEAS OPS (2225): EMS (Tippecanoe Emergency Ambulance Service)
- TCFD DISPATCH (1827) / LFD DISPATCH (1901) / WLFD DISPATCH (2021) / PUFD DISPATCH (2105): Fire
- TCSD DISPATCH (1813): Tippecanoe County Sheriff
- LPD DISPATCH (1931): Lafayette Police Department
- WLPD DISPATCH (2019): West Lafayette Police Department
- PUPD DISPATCH (2119): Purdue University Police

Common codes used in this area:
10-4=acknowledged, 10-7=out of service, 10-8=in service, 10-9=repeat, 10-20=location,
10-22=disregard, 10-23=arrived at scene, 10-27=driver license check, 10-28=vehicle registration,
10-29=wants/warrants check, 10-33=emergency/all units clear channel, 10-50=traffic accident,
10-52=ambulance needed, 10-54=possible dead body, 10-55=suspected DUI,
10-57=hit and run, 10-62=unable to copy, 10-78=need assistance, 10-79=notify coroner,
Signal 1=en route, Signal 4=arrived, Code 3=lights and siren.
Indiana uses both 10-codes and plain language depending on agency.
Adam/Boy/Charles/David/Edward/Frank/George = phonetic alphabet for beat/unit numbers.
Units are typically identified by agency prefix + number (e.g. L-14 = Lafayette unit 14).

Radio traffic since last summary (format: [HH:MM:SS] [TALKGROUP] transcript):
{block}

Summarize what has been happening. Group by incident where possible. Translate codes into plain language. Note any ongoing or unresolved situations. Be direct — this is for situational awareness."""

    client = anthropic.Anthropic()
    print("\nAsking Claude...\n")

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        summary_text = ""
        for text in stream.text_stream:
            print(text, end="", flush=True)
            summary_text += text

    print("\n")

    # Append summary to log with marker
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{SUMMARY_MARKER} {ts}\n")
        f.write(summary_text.strip() + "\n")
        f.write(f"{'=' * 40}\n\n")


def main():
    print(f"P25 summarizer — watching {LOG_FILE}")
    print("Press Enter to summarize traffic since last summary. Ctrl-C to quit.\n")
    try:
        while True:
            input("[ Enter to summarize ] ")
            note = get_note()
            lines = read_since_last_summary()
            print(f"  {len(lines)} lines of traffic to summarize.")
            request_summary(lines, note)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
