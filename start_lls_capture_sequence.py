from __future__ import annotations

import argparse
import csv
import json
import logging
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from SBAccess import SBAccess


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 65432
DEFAULT_POLL_SECONDS = 0.2
DEFAULT_START_TIMEOUT_SECONDS = 300.0
DEFAULT_SLIDE_TIMEOUT_SECONDS = 300.0


@dataclass
class CaptureTiming:
    sequence_index: int
    pair_index: int
    label: str
    xml_path: str
    command_sent_iso: str
    command_returned_iso: Optional[str]
    capture_started_iso: Optional[str]
    capture_ended_iso: Optional[str]
    slide_available_iso: Optional[str]
    command_return_seconds: Optional[float]
    start_latency_seconds: Optional[float]
    capture_duration_seconds: Optional[float]
    slide_available_after_end_seconds: Optional[float]
    command_to_slide_available_seconds: Optional[float]
    captures_before: Optional[int]
    captures_after: Optional[int]
    result: str
    error: Optional[str]


def monotonic() -> float:
    return time.perf_counter()


def timestamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def elapsed(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    return end - start


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    real_values = [value for value in values if value is not None]
    if not real_values:
        return None
    return sum(real_values) / len(real_values)


def get_num_captures(sb: SBAccess) -> Optional[int]:
    try:
        return int(sb.GetNumCaptures())
    except Exception:
        logging.exception("GetNumCaptures failed")
        return None


def wait_until_not_capturing(sb: SBAccess, poll_seconds: float, log_polls: bool) -> None:
    while True:
        is_capturing = bool(sb.IsCapturing())
        if log_polls:
            logging.info("IsCapturing before next command: %s", is_capturing)
        if not is_capturing:
            return
        time.sleep(poll_seconds)


def start_lls_capture(sb: SBAccess, xml_path: Path) -> int:
    xml_text = xml_path.read_text(encoding="utf-8")
    if not xml_text.strip():
        raise ValueError(f"XML file is empty: {xml_path}")
    return int(sb.StartLLSCapture(xml_text))


def run_one_capture(
    sb: SBAccess,
    *,
    sequence_index: int,
    pair_index: int,
    label: str,
    xml_path: Path,
    poll_seconds: float,
    start_timeout_seconds: float,
    slide_timeout_seconds: float,
    log_polls: bool,
) -> CaptureTiming:
    logging.info("Preparing %s capture %d from %s", label, sequence_index, xml_path)
    wait_until_not_capturing(sb, poll_seconds, log_polls)

    captures_before = get_num_captures(sb)

    command_sent_mono = monotonic()
    command_sent_iso = timestamp()
    logging.info(
        "COMMAND_SENT sequence=%d pair=%d label=%s xml=%s",
        sequence_index,
        pair_index,
        label,
        xml_path,
    )

    command_returned_mono: Optional[float] = None
    command_returned_iso: Optional[str] = None
    capture_started_mono: Optional[float] = None
    capture_started_iso: Optional[str] = None
    capture_ended_mono: Optional[float] = None
    capture_ended_iso: Optional[str] = None
    slide_available_mono: Optional[float] = None
    slide_available_iso: Optional[str] = None
    captures_after: Optional[int] = None
    result = "ok"
    error: Optional[str] = None

    try:
        capture_id = start_lls_capture(sb, xml_path)
        logging.info(
            "START_LLS_CAPTURE_RETURNED sequence=%d label=%s capture_id=%s",
            sequence_index,
            label,
            capture_id,
        )
        command_returned_mono = monotonic()
        command_returned_iso = timestamp()
        logging.info(
            "COMMAND_RETURNED sequence=%d label=%s elapsed=%.3fs",
            sequence_index,
            label,
            command_returned_mono - command_sent_mono,
        )

        deadline = command_sent_mono + start_timeout_seconds
        while monotonic() <= deadline:
            is_capturing = bool(sb.IsCapturing())
            if log_polls:
                logging.info(
                    "IsCapturing sequence=%d label=%s value=%s",
                    sequence_index,
                    label,
                    is_capturing,
                )
            if is_capturing:
                capture_started_mono = monotonic()
                capture_started_iso = timestamp()
                logging.info(
                    "CAPTURE_STARTED sequence=%d label=%s start_latency=%.3fs",
                    sequence_index,
                    label,
                    capture_started_mono - command_sent_mono,
                )
                break
            time.sleep(poll_seconds)

        if capture_started_mono is None:
            raise TimeoutError(
                f"Capture did not start within {start_timeout_seconds:.1f}s"
            )

        while True:
            is_capturing = bool(sb.IsCapturing())
            if log_polls:
                logging.info(
                    "IsCapturing sequence=%d label=%s value=%s",
                    sequence_index,
                    label,
                    is_capturing,
                )
            if not is_capturing:
                capture_ended_mono = monotonic()
                capture_ended_iso = timestamp()
                logging.info(
                    "CAPTURE_ENDED sequence=%d label=%s duration=%.3fs",
                    sequence_index,
                    label,
                    capture_ended_mono - capture_started_mono,
                )
                break
            time.sleep(poll_seconds)

        slide_deadline = monotonic() + slide_timeout_seconds
        while monotonic() <= slide_deadline:
            captures_after = get_num_captures(sb)
            logging.info(
                "GetNumCaptures sequence=%d label=%s before=%s after=%s",
                sequence_index,
                label,
                captures_before,
                captures_after,
            )
            if (
                captures_before is None
                or captures_after is None
                or captures_after > captures_before
            ):
                slide_available_mono = monotonic()
                slide_available_iso = timestamp()
                logging.info(
                    "SLIDE_AVAILABLE sequence=%d label=%s after_end=%.3fs",
                    sequence_index,
                    label,
                    slide_available_mono - capture_ended_mono,
                )
                break
            time.sleep(poll_seconds)

        if slide_available_mono is None:
            result = "slide_timeout"
            logging.warning(
                "Capture ended but no new capture count appeared within %.1fs",
                slide_timeout_seconds,
            )

    except Exception as exc:
        result = "error"
        error = str(exc)
        logging.exception("Capture sequence=%d label=%s failed", sequence_index, label)

    return CaptureTiming(
        sequence_index=sequence_index,
        pair_index=pair_index,
        label=label,
        xml_path=str(xml_path),
        command_sent_iso=command_sent_iso,
        command_returned_iso=command_returned_iso,
        capture_started_iso=capture_started_iso,
        capture_ended_iso=capture_ended_iso,
        slide_available_iso=slide_available_iso,
        command_return_seconds=elapsed(command_sent_mono, command_returned_mono),
        start_latency_seconds=elapsed(command_sent_mono, capture_started_mono),
        capture_duration_seconds=elapsed(capture_started_mono, capture_ended_mono),
        slide_available_after_end_seconds=elapsed(
            capture_ended_mono, slide_available_mono
        ),
        command_to_slide_available_seconds=elapsed(command_sent_mono, slide_available_mono),
        captures_before=captures_before,
        captures_after=captures_after,
        result=result,
        error=error,
    )


def write_csv(path: Path, timings: list[CaptureTiming]) -> None:
    if not timings:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(timings[0]).keys()))
        writer.writeheader()
        for timing in timings:
            writer.writerow(asdict(timing))


def build_summary(timings: list[CaptureTiming]) -> dict[str, object]:
    command_sent_times = [
        datetime.fromisoformat(timing.command_sent_iso) for timing in timings
    ]
    command_intervals = [
        (command_sent_times[index] - command_sent_times[index - 1]).total_seconds()
        for index in range(1, len(command_sent_times))
    ]
    first_command = command_sent_times[0] if command_sent_times else None
    ended_times = [
        datetime.fromisoformat(timing.capture_ended_iso)
        for timing in timings
        if timing.capture_ended_iso
    ]
    last_capture_end = max(ended_times) if ended_times else None

    total_time = None
    if first_command is not None and last_capture_end is not None:
        total_time = (last_capture_end - first_command).total_seconds()

    return {
        "captures_requested": len(timings),
        "captures_ok": sum(1 for timing in timings if timing.result == "ok"),
        "captures_with_errors": sum(1 for timing in timings if timing.result == "error"),
        "captures_with_slide_timeout": sum(
            1 for timing in timings if timing.result == "slide_timeout"
        ),
        "average_seconds_between_command_sent": mean(command_intervals),
        "total_seconds_first_command_to_last_capture_end": total_time,
        "average_start_latency_seconds": mean(
            timing.start_latency_seconds for timing in timings
        ),
        "average_capture_duration_seconds": mean(
            timing.capture_duration_seconds for timing in timings
        ),
        "average_slide_available_after_end_seconds": mean(
            timing.slide_available_after_end_seconds for timing in timings
        ),
        "average_command_to_slide_available_seconds": mean(
            timing.command_to_slide_available_seconds for timing in timings
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Alternate two SlideBook LLS XML captures and time capture start/end "
            "using only SBAccess calls."
        )
    )
    parser.add_argument("--xml-a", default=None, help="Path to the first XML script.")
    parser.add_argument("--xml-b", default=None, help="Path to the second XML script.")
    parser.add_argument("--label-a", default="A", help="Label for the first XML script.")
    parser.add_argument("--label-b", default="B", help="Label for the second XML script.")
    parser.add_argument(
        "--repeat",
        type=int,
        default=10,
        help="Number of A/B pairs to run. Default: 10.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="SlideBook host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="SlideBook port.")
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=60.0,
        help="Socket timeout in seconds for SBAccess calls.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Seconds between IsCapturing polls.",
    )
    parser.add_argument(
        "--start-timeout-seconds",
        type=float,
        default=DEFAULT_START_TIMEOUT_SECONDS,
        help="Maximum wait for IsCapturing to become true after command sent.",
    )
    parser.add_argument(
        "--slide-timeout-seconds",
        type=float,
        default=DEFAULT_SLIDE_TIMEOUT_SECONDS,
        help="Maximum wait for GetNumCaptures to increase after IsCapturing ends.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for log, csv, and summary files. Default: this script folder.",
    )
    parser.add_argument(
        "--no-poll-log",
        action="store_true",
        help="Do not write every IsCapturing poll to the detailed log.",
    )
    return parser.parse_args()


def main() -> int:
    # Hard-code your XML paths here if you do not want to pass --xml-a/--xml-b.
    # CLI values still win when supplied.
    hardcoded_xml_a = r"C:\Users\3i\Documents\GitHub\3iSynergyLLS\488_only.xml"
    hardcoded_xml_b = r"C:\Users\3i\Documents\GitHub\3iSynergyLLS\488_560.xml"

    args = parse_args()
    xml_a_value = args.xml_a or hardcoded_xml_a
    xml_b_value = args.xml_b or hardcoded_xml_b
    if not xml_a_value:
        print("Set hardcoded_xml_a in main() or pass --xml-a", file=sys.stderr)
        return 2
    if not xml_b_value:
        print("Set hardcoded_xml_b in main() or pass --xml-b", file=sys.stderr)
        return 2

    xml_a = Path(xml_a_value).expanduser().resolve()
    xml_b = Path(xml_b_value).expanduser().resolve()
    if not xml_a.exists():
        print(f"XML A not found: {xml_a}", file=sys.stderr)
        return 2
    if not xml_b.exists():
        print(f"XML B not found: {xml_b}", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else Path(__file__).resolve().parent
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"lls_capture_sequence_{run_stamp}.log"
    csv_path = output_dir / f"lls_capture_sequence_{run_stamp}.csv"
    summary_path = output_dir / f"lls_capture_sequence_{run_stamp}_summary.json"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    timings: list[CaptureTiming] = []
    sock: Optional[socket.socket] = None
    try:
        logging.info("Connecting to SlideBook at %s:%d", args.host, args.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(float(args.socket_timeout))
        sock.connect((args.host, int(args.port)))
        sb = SBAccess(sock)
        logging.info("Connected")

        sequence_index = 0
        for pair_index in range(1, args.repeat + 1):
            for label, xml_path in ((args.label_a, xml_a), (args.label_b, xml_b)):
                sequence_index += 1
                timing = run_one_capture(
                    sb,
                    sequence_index=sequence_index,
                    pair_index=pair_index,
                    label=label,
                    xml_path=xml_path,
                    poll_seconds=float(args.poll_seconds),
                    start_timeout_seconds=float(args.start_timeout_seconds),
                    slide_timeout_seconds=float(args.slide_timeout_seconds),
                    log_polls=not bool(args.no_poll_log),
                )
                timings.append(timing)
                if timing.result == "error":
                    break
            if timings and timings[-1].result == "error":
                break

    finally:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    write_csv(csv_path, timings)
    summary = build_summary(timings)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    logging.info("Wrote detailed log: %s", log_path)
    logging.info("Wrote timing CSV: %s", csv_path)
    logging.info("Wrote summary JSON: %s", summary_path)
    logging.info("SUMMARY %s", json.dumps(summary, sort_keys=True))

    return 1 if any(timing.result == "error" for timing in timings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
