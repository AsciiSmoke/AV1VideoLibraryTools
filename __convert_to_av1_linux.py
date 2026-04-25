"""
convert_to_av1_linux.py

Scans a folder for MKV/MP4 files that are not already AV1 encoded
or have a video bitrate above a threshold, then converts them
using HandBrakeCLI with AV1 SVT 10-bit settings.

Install dependencies (Arch / CachyOS):
    sudo pacman -S handbrake-cli ffmpeg

Usage:
    python __convert_to_av1_linux.py <folder> [options]

Examples:
    python __convert_to_av1_linux.py
    python __convert_to_av1_linux.py --dry-run
    python __convert_to_av1_linux.py ~/Videos
    python __convert_to_av1_linux.py ~/Videos --bitrate-threshold 8000
    python __convert_to_av1_linux.py /mnt/nas/videos --handbrake /usr/local/bin/HandBrakeCLI
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults — override via CLI args
# ---------------------------------------------------------------------------

DEFAULT_HANDBRAKE   = "HandBrakeCLI"   # assumed on PATH after: sudo pacman -S handbrake-cli
DEFAULT_FFPROBE     = "ffprobe"        # assumed on PATH after: sudo pacman -S ffmpeg

# Bitrate thresholds by resolution — files above these are candidates for conversion
THRESHOLD_SD        =  8_000      # kbps — below 720p
THRESHOLD_HD        = 13_000      # kbps — 720p to 1080p
THRESHOLD_UHD       = 18_000      # kbps — above 1080p

SUPPORTED_EXTS      = {".mkv", ".mp4"}
AV1_CODEC_NAMES     = {"av1", "libaom-av1", "svt-av1"}


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path | None) -> logging.Logger:
    logger = logging.getLogger("av1_converter")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_path is not None:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Video probing
# ---------------------------------------------------------------------------

def probe_video(path: Path, ffprobe_exe: str) -> dict | None:
    """
    Returns a dict with keys:
        codec       str   e.g. "h264", "hevc", "av1"
        bitrate     int   video stream bitrate in kbps (0 if unknown)
        width       int
        height      int
        fps         str   e.g. "23.976"
        bit_depth   int   e.g. 8 or 10
        file_size   int   bytes
    Returns None on probe failure.
    """
    cmd = [
        ffprobe_exe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"ffprobe not found at '{ffprobe_exe}'. "
            "Install ffmpeg or pass --ffprobe with the correct path."
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )

    if not video_stream:
        return None

    # Bitrate: prefer stream-level, fall back to format-level, then 0
    raw_bitrate = (
        video_stream.get("bit_rate")
        or data.get("format", {}).get("bit_rate")
        or "0"
    )
    bitrate_kbps = int(raw_bitrate) // 1000

    # FPS as a readable decimal
    fps_raw = video_stream.get("avg_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = round(int(num) / int(den), 3) if int(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    pix_fmt  = video_stream.get("pix_fmt", "")
    bit_depth = 10 if "10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt else 8

    return {
        "codec":     video_stream.get("codec_name", "unknown").lower(),
        "bitrate":   bitrate_kbps,
        "width":     int(video_stream.get("width", 0)),
        "height":    int(video_stream.get("height", 0)),
        "fps":       fps,
        "bit_depth": bit_depth,
        "file_size": path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Conversion decision
# ---------------------------------------------------------------------------

def bitrate_threshold_for(width: int) -> tuple[int, str]:
    """Returns (threshold_kbps, label) based on horizontal resolution."""
    if width < 1280:
        return THRESHOLD_SD,  "SD (<720p)"
    if width < 1920:
        return THRESHOLD_HD,  "HD (<1080p)"
    return THRESHOLD_UHD, "UHD (>1080p)"


def needs_conversion(info: dict) -> tuple[bool, str]:
    """
    Returns (should_convert, reason_string).
    Converts only if the file is both non-AV1 AND above the
    bitrate threshold for its resolution.
    """
    if info["codec"] in AV1_CODEC_NAMES:
        return False, "already AV1"

    threshold, res_label = bitrate_threshold_for(info["width"])

    if info["bitrate"] <= threshold:
        return False, (
            f"codec is {info['codec']!r} but bitrate {info['bitrate']:,} kbps "
            f"is within {res_label} threshold ({threshold:,} kbps)"
        )

    return True, (
        f"codec is {info['codec']!r} (not AV1); "
        f"bitrate {info['bitrate']:,} kbps > {res_label} threshold ({threshold:,} kbps)"
    )


# ---------------------------------------------------------------------------
# MP4 → MKV remux (no transcode)
# ---------------------------------------------------------------------------

def remux_to_mkv(
    input_path: Path,
    ffprobe_exe: str,
    logger: logging.Logger,
    dry_run: bool = False,
) -> bool:
    """
    Remuxes an MP4 file into an MKV container in-place using stream copy.
    The original .mp4 is deleted on success.
    Returns True on success, False on failure.
    """
    if input_path.suffix.lower() != ".mp4":
        return False

    output_path = input_path.with_suffix(".mkv")
    temp_path   = input_path.with_suffix(".remux_temp.mkv")

    # Derive ffmpeg path from ffprobe path
    ffmpeg_exe = ffprobe_exe.replace("ffprobe", "ffmpeg")

    cmd = [
        ffmpeg_exe,
        "-i",       str(input_path),
        "-c",       "copy",          # copy all streams, no transcode
        "-y",                        # overwrite temp if it exists
        str(temp_path),
    ]

    logger.info("  REMUX → %s (MP4 → MKV, no transcode)", output_path.name)
    logger.debug("ffmpeg command: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))

    if dry_run:
        logger.info("  [DRY RUN] Would remux: %s → %s", input_path.name, output_path.name)
        return True

    try:
        process = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        logger.error("  ffmpeg not found at '%s' — cannot remux", ffmpeg_exe)
        return False

    if process.returncode != 0 or not temp_path.exists():
        logger.error("  REMUX FAILED for %s", input_path.name)
        logger.debug("  ffmpeg stderr: %s", process.stderr[-2000:])
        temp_path.unlink(missing_ok=True)
        return False

    temp_path.replace(output_path)
    input_path.unlink()
    return True


# ---------------------------------------------------------------------------
# HandBrake conversion
# ---------------------------------------------------------------------------

def build_handbrake_cmd(
    input_path: Path,
    output_path: Path,
    handbrake_exe: str,
    bit_depth: int = 10,
) -> list[str]:
    encoder = "svt_av1_10bit" if bit_depth >= 10 else "svt_av1"  # Software SVT-AV1 (AMD VCN not available via HandBrake on Linux)
    return [
        handbrake_exe,
        "--input",   str(input_path),
        "--output",  str(output_path),

        # Preset (base; overrides below take precedence)
        "--preset",  "AV1 MKV 2160p60 4K",

        # Video
        "--encoder",        encoder,
        "--quality",        "28",
        "--vfr",                             # Variable framerate (same as source)

        # Audio: AAC, same channels, English only
        "--aencoder",       "av_aac",
        "--mixdown",        "none",          # preserve source channel layout
        "--audio-lang-list", "eng",

        # Subtitles: English only, passthrough
        "--subtitle-lang-list", "eng",
        "--subtitle-default", "none",

        # Metadata
        "--optimize",                        # web-optimised MP4/MKV
    ]


def convert_file(
    input_path: Path,
    handbrake_exe: str,
    logger: logging.Logger,
    bit_depth: int = 10,
    dry_run: bool = False,
) -> tuple[bool, Path | None]:
    """
    Converts input_path and writes the result into a 'converted' subfolder
    inside the same directory as the source file.
    The original file is never modified or deleted.
    Returns (success, output_path).
    """
    output_folder = input_path.parent / ".converted"
    output_folder.mkdir(parents=True, exist_ok=True)

    # Always output as .mkv; use a temp name until HandBrake finishes
    final_output = output_folder / input_path.with_suffix(".mkv").name
    temp_output  = output_folder / input_path.with_suffix(".av1_temp.mkv").name

    cmd = build_handbrake_cmd(input_path, temp_output, handbrake_exe, bit_depth)

    logger.debug("HandBrake command: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))

    if dry_run:
        logger.info("  [DRY RUN] Would run: %s", " ".join(cmd[:4]) + " ...")
        return True, None

    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"HandBrakeCLI not found at '{handbrake_exe}'. "
            "Pass --handbrake with the correct path."
        )

    if process.returncode != 0:
        logger.error("  HandBrake exited with code %d", process.returncode)
        if process.stderr:
            logger.error("  HandBrake stderr:\n%s", process.stderr[-3000:])
        if temp_output.exists():
            temp_output.unlink()
        return False, None

    if not temp_output.exists():
        logger.error("  HandBrake exited cleanly but produced no output file.")
        if process.stderr:
            logger.error("  HandBrake stderr:\n%s", process.stderr[-3000:])
        return False, None

    temp_output.replace(final_output)
    return True, final_output


# ---------------------------------------------------------------------------
# JSON activity log
# ---------------------------------------------------------------------------

class ActivityLog:
    def __init__(self, log_path: Path):
        self._path = log_path
        self._entries: list[dict] = []

        if log_path.exists():
            try:
                with open(log_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, list):
                    self._entries = existing
            except (json.JSONDecodeError, OSError):
                pass


    def add(self, entry: dict):
        self._entries.append(entry)
        self._flush()


    def _flush(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def scan_and_convert(args: argparse.Namespace):
    scan_folder = Path(args.folder).resolve()
    if not scan_folder.is_dir():
        print(f"ERROR: Folder not found: {scan_folder}", file=sys.stderr)
        sys.exit(1)

    timestamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_log_path = scan_folder / f"_av1_conversion_linux_{timestamp}.json"
    text_log_path = scan_folder / f"_av1_conversion_linux_{timestamp}.log" if args.log else None

    logger       = setup_logging(text_log_path)
    activity_log = ActivityLog(json_log_path)

    logger.info("=" * 70)
    logger.info("AV1 Conversion Script")
    logger.info("Scan folder : %s", scan_folder)
    logger.info("Output      : <source dir>/converted/")
    logger.info("Thresholds  : SD <720p=%s kbps  HD <1080p=%s kbps  UHD >1080p=%s kbps",
                f"{THRESHOLD_SD:,}", f"{THRESHOLD_HD:,}", f"{THRESHOLD_UHD:,}")
    logger.info("HandBrake   : %s", args.handbrake)
    logger.info("ffprobe     : %s", args.ffprobe)
    logger.info("Dry run     : %s", args.dry_run)
    logger.info("Log file    : %s", text_log_path.name if text_log_path else "disabled")
    logger.info("=" * 70)

    # Collect candidate files, skipping anything already inside a 'converted' folder
    candidates = sorted(
        p.resolve() for p in scan_folder.rglob("*")
        if p.suffix.lower() in SUPPORTED_EXTS
        and ".converted" not in p.parts
    )

    if not candidates:
        logger.info("No MKV/MP4 files found in %s", scan_folder)
        return

    logger.info("Found %d video file(s) to examine.", len(candidates))

    files_scanned    = 0
    files_queued     = 0
    files_converted  = 0
    files_accepted   = 0
    files_rejected   = 0
    files_failed     = 0

    for video_path in candidates:
        files_scanned += 1
        logger.info("")
        logger.info("[%d/%d] Probing: %s", files_scanned, len(candidates), video_path.name)

        try:
            info = probe_video(video_path, args.ffprobe)
        except RuntimeError as e:
            logger.error("  %s", e)
            sys.exit(1)

        if info is None:
            logger.warning("  Could not probe file — skipping.")
            activity_log.add({
                "file":   str(video_path),
                "action": "skipped",
                "reason": "probe failed",
                "time":   datetime.now().isoformat(),
            })
            continue

        logger.info(
            "  Codec: %s | Bitrate: %s kbps | Resolution: %dx%d | "
            "FPS: %s | Bit-depth: %d-bit | Size: %s",
            info["codec"],
            f"{info['bitrate']:,}",
            info["width"], info["height"],
            info["fps"],
            info["bit_depth"],
            _fmt_size(info["file_size"]),
        )

        should_convert, reason = needs_conversion(info)

        if not should_convert:
            logger.info("  OK — no conversion needed.")
            if video_path.suffix.lower() == ".mp4":
                remux_to_mkv(video_path, args.ffprobe, logger, dry_run=args.dry_run)
            activity_log.add({
                "file":          str(video_path),
                "action":        "skipped",
                "reason":        "already AV1 and within bitrate threshold",
                "source_info":   info,
                "time":          datetime.now().isoformat(),
            })
            continue

        files_queued += 1
        logger.info("  QUEUED for conversion: %s", reason)

        success, output_path = convert_file(
            video_path,
            args.handbrake,
            logger,
            bit_depth=info["bit_depth"],
            dry_run=args.dry_run,
        )

        if args.dry_run:
            activity_log.add({
                "file":        str(video_path),
                "action":      "dry_run",
                "reason":      reason,
                "source_info": info,
                "time":        datetime.now().isoformat(),
            })
            continue

        if success and output_path:
            files_converted += 1

            try:
                output_info = probe_video(output_path, args.ffprobe)
            except RuntimeError:
                output_info = None

            size_after = output_path.stat().st_size if output_path.exists() else 0
            saving_pct = (
                round((1 - size_after / info["file_size"]) * 100, 1)
                if info["file_size"] > 0 else 0
            )

            logger.info("  SUCCESS → %s", output_path.name)
            if output_info:
                logger.info(
                    "  Output: codec=%s bitrate=%s kbps size=%s (saved %s%%)",
                    output_info["codec"],
                    f"{output_info['bitrate']:,}",
                    _fmt_size(size_after),
                    saving_pct,
                )

            # Auto-accept: overwrite source if output is at least 10% smaller
            if args.auto_accept_if_smaller:
                if saving_pct >= 10.0:
                    files_accepted += 1
                    final_dest = video_path.with_suffix(".mkv")
                    output_path.replace(final_dest)
                    if video_path != final_dest and video_path.exists():
                        video_path.unlink()
                    logger.info(
                        "  AUTO-ACCEPT: source replaced with converted file (%.1f%% smaller)",
                        saving_pct,
                    )
                    activity_log.add({
                        "file":            str(video_path),
                        "output_file":     str(final_dest),
                        "action":          "converted+accepted",
                        "reason":          reason,
                        "source_info":     info,
                        "output_info":     output_info,
                        "size_saving_pct": saving_pct,
                        "time":            datetime.now().isoformat(),
                    })
                else:
                    files_rejected += 1
                    output_path.unlink(missing_ok=True)
                    logger.info(
                        "  AUTO-REJECT: output only %.1f%% smaller (threshold 10%%) — discarded",
                        saving_pct,
                    )
                    if video_path.suffix.lower() == ".mp4":
                        remux_to_mkv(video_path, args.ffprobe, logger, dry_run=args.dry_run)
                    activity_log.add({
                        "file":            str(video_path),
                        "action":          "converted+rejected",
                        "reason":          reason,
                        "source_info":     info,
                        "output_info":     output_info,
                        "size_saving_pct": saving_pct,
                        "time":            datetime.now().isoformat(),
                    })
            else:
                activity_log.add({
                    "file":            str(video_path),
                    "output_file":     str(output_path),
                    "action":          "converted",
                    "reason":          reason,
                    "source_info":     info,
                    "output_info":     output_info,
                    "size_saving_pct": saving_pct,
                    "time":            datetime.now().isoformat(),
                })

        else:
            files_failed += 1
            logger.error("  FAILED to convert %s", video_path.name)
            activity_log.add({
                "file":        str(video_path),
                "action":      "failed",
                "reason":      reason,
                "source_info": info,
                "time":        datetime.now().isoformat(),
            })

        logger.info("-" * 70)

    # Clean up any empty .converted folders left behind
    for converted_dir in scan_folder.rglob(".converted"):
        if converted_dir.is_dir():
            try:
                converted_dir.rmdir()   # only removes if empty
                logger.info("Removed empty folder: %s", converted_dir)
            except OSError:
                pass                    # not empty — leave it alone

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("Done.")
    logger.info("  Scanned   : %d", files_scanned)
    logger.info("  Queued    : %d", files_queued)
    logger.info("  Converted : %d", files_converted)
    if args.auto_accept_if_smaller:
        logger.info("  Accepted  : %d", files_accepted)
        logger.info("  Rejected  : %d", files_rejected)
    logger.info("  Failed    : %d", files_failed)
    logger.info("  Skipped   : %d", files_scanned - files_queued)
    logger.info("JSON log    : %s", json_log_path.name)
    if text_log_path:
        logger.info("Log file    : %s", text_log_path.name)
    logger.info("=" * 70)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan a folder for non-AV1 or high-bitrate videos and convert with HandBrake.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "folder",
        nargs="?",
        default=".",
        help="Folder to scan (searched recursively). Defaults to the current directory.",
    )
    parser.add_argument(
        "--handbrake",
        default=DEFAULT_HANDBRAKE,
        metavar="PATH",
        help=f"Path to HandBrakeCLI (default: '{DEFAULT_HANDBRAKE}', assumed on PATH)",
    )
    parser.add_argument(
        "--ffprobe",
        default=DEFAULT_FFPROBE,
        metavar="PATH",
        help=f"Path to ffprobe (default: '{DEFAULT_FFPROBE}', assumed on PATH)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and log what would be converted without actually running HandBrake.",
    )
    parser.add_argument(
        "--auto-accept-if-smaller",
        action="store_true",
        help=(
            "If the converted file is at least 10%% smaller than the source, "
            "overwrite the source with it. Files that don't meet the threshold "
            "are discarded. Empty .converted folders are cleaned up at the end."
        ),
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Write a .log text file in addition to the JSON activity log.",
    )

    args = parser.parse_args()
    scan_and_convert(args)


if __name__ == "__main__":
    main()