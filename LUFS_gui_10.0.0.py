import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import csv
import os
import re
import json
import math
import time
import shutil
from datetime import datetime
from statistics import mean
from concurrent.futures import ThreadPoolExecutor, as_completed

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".dsf"}
FFMPEG_CMD = "ffmpeg"
FFPROBE_CMD = "ffprobe"
TARGET_LUFS = -14.0
TARGET_TP = -1.0
TARGET_LRA = 11.0
LUFS_PRESETS = [
    ("streaming_14", "Spotify / YouTube (-14 LUFS)", -14.0),
    ("apple_music", "Apple Music (-16 LUFS)", -16.0),
    ("broadcast", "Broadcast / EBU R128 (-23 LUFS)", -23.0),
]
CUSTOM_LUFS_PRESET = "custom"
SILENCE_LUFS = -70.0   # anything <= this is treated as silence
FATIGUE_LRA_SAFE = 6.5
FATIGUE_LRA_HEAVY = 2.0
FATIGUE_THRESH_SAFE = -28.0
FATIGUE_THRESH_HEAVY = -20.0
ALBUM_HIGH_RISK_WEIGHT = 0.15
ALBUM_FLAT_WEIGHT = 0.10
ALBUM_TP_DAMAGE_WEIGHT = 0.15
ALBUM_NORMALIZATION_WEIGHT = 0.10
TP_SEVERE_FLOOR_RATIO = 0.60
TP_SEVERE_FLOOR_PEAK = 1.5
TP_CONTEXT_FLOOR_RATIO = 0.35
TP_CONTEXT_FLOOR_PEAK = 0.7
TP_CONTEXT_CORE_FATIGUE = 35.0
TP_CONTEXT_NORMALIZATION = 45.0
APP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "LUFS Inspector",
)
KEEP_DIR = os.path.join(APP_DIR, "kept_results")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
KEEP_SORT_FIELDS = ("Measured Date", "Artist", "Ear Fatigue Score")
KEEP_SORT_ORDERS = ("Descending", "Ascending")
RESULTS_MIN_WIDTH = 320
KEEP_MIN_WIDTH = 360
KEEP_DEFAULT_WIDTH = 520

def time_weighted_avg(results, key):
    total_time = sum(r["duration"] for r in results if r["duration"] > 0)
    if total_time <= 0:
        return None
    return sum(
        r[key] * r["duration"]
        for r in results
        if r["duration"] > 0
    ) / total_time

def clamp(value, low, high):
    return max(low, min(high, value))

def target_lufs(target):
    return float((target or {}).get("lufs", TARGET_LUFS))

def target_tp(target):
    return float((target or {}).get("tp", TARGET_TP))

def target_lra(target):
    return float((target or {}).get("lra", TARGET_LRA))

def missing_audio_tools():
    return [
        cmd
        for cmd in (FFMPEG_CMD, FFPROBE_CMD)
        if shutil.which(cmd) is None
    ]

def audio_tools_error_message(missing):
    tools = ", ".join(missing)
    return (
        f"Required audio tool not found: {tools}\n\n"
        "Please install FFmpeg and make sure both ffmpeg and ffprobe are available in PATH.\n"
        "On Windows, ffmpeg.exe and ffprobe.exe usually need to be in the same folder, "
        "and that folder must be added to PATH."
    )

def track_fatigue_risk(output_lra, output_threshold):
    lra_risk = clamp(
        (FATIGUE_LRA_SAFE - output_lra) / (FATIGUE_LRA_SAFE - FATIGUE_LRA_HEAVY),
        0.0,
        1.0,
    )
    threshold_risk = clamp(
        (output_threshold - FATIGUE_THRESH_SAFE) / (FATIGUE_THRESH_HEAVY - FATIGUE_THRESH_SAFE),
        0.0,
        1.0,
    )
    combo_risk = lra_risk * threshold_risk
    fatigue = 100.0 * (
        0.65 * lra_risk +
        0.25 * threshold_risk +
        0.10 * combo_risk
    )
    return fatigue

def ratio_penalty(ratio):
    return 100.0 * math.sqrt(clamp(ratio, 0.0, 1.0))

def input_tp_damage_score(tp_over_ratio, input_tp_severity):
    return 100.0 * clamp(
        0.55 * math.sqrt(clamp(tp_over_ratio, 0.0, 1.0)) +
        0.45 * clamp(input_tp_severity, 0.0, 1.0),
        0.0,
        1.0,
    )

def album_fatigue_score(
    core_album_fatigue,
    high_risk_score,
    flat_score,
    tp_damage_score,
    album_normalization_penalty,
    high_risk_ratio,
    flat_ratio,
    tp_over_ratio,
    max_input_tp,
):
    score = clamp(
        core_album_fatigue +
        ALBUM_HIGH_RISK_WEIGHT * high_risk_score +
        ALBUM_FLAT_WEIGHT * flat_score +
        ALBUM_TP_DAMAGE_WEIGHT * tp_damage_score +
        ALBUM_NORMALIZATION_WEIGHT * album_normalization_penalty,
        0.0,
        100.0,
    )

    if high_risk_ratio >= 0.50:
        score = max(score, 65.0)
    if high_risk_ratio >= 0.25:
        score = max(score, 45.0)
    if flat_ratio >= 0.50:
        score = max(score, 45.0)
    if tp_over_ratio >= TP_SEVERE_FLOOR_RATIO and max_input_tp >= TP_SEVERE_FLOOR_PEAK:
        score = max(score, 45.0)
    if (
        tp_over_ratio >= TP_CONTEXT_FLOOR_RATIO
        and max_input_tp >= TP_CONTEXT_FLOOR_PEAK
        and (
            core_album_fatigue >= TP_CONTEXT_CORE_FATIGUE
            or album_normalization_penalty >= TP_CONTEXT_NORMALIZATION
        )
    ):
        score = max(score, 45.0)

    return score

def normalization_penalty(input_integrated, input_true_peak, target=None):
    if input_integrated is None or input_true_peak is None:
        return 0.0

    down_gain = max(0.0, input_integrated - target_lufs(target))
    loudness_risk = clamp((down_gain - 3.0) / 7.0, 0.0, 1.0)
    peak_risk = clamp((input_true_peak + 1.0) / 1.0, 0.0, 1.0)
    return 100.0 * loudness_risk * peak_risk

def compute_album_fatigue_metrics(results, total_time, max_input_tp, target=None):
    fatigue_tracks = [
        (
            track_fatigue_risk(
                r["Output LRA"],
                r["Output Threshold"],
            ),
            r["duration"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    core_album_fatigue = sum(
        risk * duration
        for risk, duration, _ in fatigue_tracks
    ) / total_time
    high_risk_time = sum(
        duration
        for risk, duration, _ in fatigue_tracks
        if risk >= 65.0
    )
    high_risk_ratio = high_risk_time / total_time
    high_risk_score = ratio_penalty(high_risk_ratio)
    peak_risk, _, peak_risk_track = max(fatigue_tracks, key=lambda x: x[0])

    clipped_tracks = [
        r for r in results
        if r["duration"] > 0 and r["Input True Peak"] > 0.0
    ]
    clipped_time = sum(r["duration"] for r in clipped_tracks)
    clipped_ratio = clipped_time / total_time
    input_tp_severity = sum(
        clamp((r["Input True Peak"] - 0.0) / 1.0, 0.0, 1.0) * r["duration"]
        for r in results
        if r["duration"] > 0
    ) / total_time
    tp_damage_score = input_tp_damage_score(clipped_ratio, input_tp_severity)

    flat_tracks = [
        r for r in results
        if r["duration"] > 0 and r["Output LRA"] < 3.0
    ]
    flat_time = sum(r["duration"] for r in flat_tracks)
    flat_ratio = flat_time / total_time
    flat_score = ratio_penalty(flat_ratio)

    normalization_penalties = [
        (
            normalization_penalty(r["Input Integrated"], r["Input True Peak"], target),
            r["duration"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    album_normalization_penalty = sum(
        penalty * duration
        for penalty, duration, _ in normalization_penalties
    ) / total_time
    peak_normalization_penalty, _, peak_normalization_track = max(
        normalization_penalties,
        key=lambda x: x[0],
    )

    album_fatigue = album_fatigue_score(
        core_album_fatigue,
        high_risk_score,
        flat_score,
        tp_damage_score,
        album_normalization_penalty,
        high_risk_ratio,
        flat_ratio,
        clipped_ratio,
        max_input_tp,
    )
    verdict, verdict_label = fatigue_verdict(album_fatigue)

    return {
        "album_fatigue": album_fatigue,
        "verdict": verdict,
        "verdict_label": verdict_label,
        "core_album_fatigue": core_album_fatigue,
        "high_risk_time": high_risk_time,
        "high_risk_ratio": high_risk_ratio,
        "high_risk_score": high_risk_score,
        "peak_risk": peak_risk,
        "peak_risk_track": peak_risk_track,
        "clipped_tracks": clipped_tracks,
        "clipped_time": clipped_time,
        "clipped_ratio": clipped_ratio,
        "tp_damage_score": tp_damage_score,
        "flat_time": flat_time,
        "flat_ratio": flat_ratio,
        "flat_score": flat_score,
        "album_normalization_penalty": album_normalization_penalty,
        "peak_normalization_penalty": peak_normalization_penalty,
        "peak_normalization_track": peak_normalization_track,
    }

def fatigue_verdict(score):
    if score >= 65.0:
        return "×", "Heavy Fatigue"
    if score >= 45.0:
        return "△", "Fatiguing"
    if score >= 25.0:
        return "O", "OK"
    return "◎", "Comfortable"

def loudnorm_result_to_json(result):
    data = {
        "input_i": result.get("Input Integrated"),
        "input_tp": result.get("Input True Peak"),
        "input_lra": result.get("Input LRA"),
        "input_thresh": result.get("Input Threshold"),
        "output_i": result.get("Output Integrated"),
        "output_tp": result.get("Output True Peak"),
        "output_lra": result.get("Output LRA"),
        "output_thresh": result.get("Output Threshold"),
        "target_offset": result.get("Target Offset"),
    }
    return json.dumps(data, indent=4)

def decode_ffmpeg_output(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")

def run_loudnorm_pass(path: str, loudnorm_args: str) -> str:
    cmd = [
        FFMPEG_CMD,
        "-i", path,
        "-filter_complex",
        loudnorm_args,
        "-f", "null",
        "-"
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(audio_tools_error_message([FFMPEG_CMD]))
    return decode_ffmpeg_output(proc.stderr or b"")

def run_loudnorm(path: str, two_pass=False, target=None) -> str:
    lufs = target_lufs(target)
    tp = target_tp(target)
    lra = target_lra(target)
    first_args = (
        f"loudnorm=I={lufs:g}:TP={tp:g}:"
        f"LRA={lra:g}:print_format=json"
    )
    first_raw = run_loudnorm_pass(path, first_args)

    if not two_pass:
        return first_raw

    first = parse_loudnorm_output(first_raw)
    required = [
        "Input Integrated",
        "Input True Peak",
        "Input LRA",
        "Input Threshold",
        "Target Offset",
    ]
    if not first or any(
        first.get(k) is None or not math.isfinite(first[k])
        for k in required
    ):
        return first_raw

    second_args = (
        f"loudnorm=I={lufs:g}:TP={tp:g}:LRA={lra:g}:"
        f"measured_I={first['Input Integrated']}:"
        f"measured_TP={first['Input True Peak']}:"
        f"measured_LRA={first['Input LRA']}:"
        f"measured_thresh={first['Input Threshold']}:"
        f"offset={first['Target Offset']}:"
        "linear=true:print_format=json"
    )
    second_raw = run_loudnorm_pass(path, second_args)
    second = parse_loudnorm_output(second_raw)
    if not second:
        return second_raw

    merged = first.copy()
    for key in [
        "Output Integrated",
        "Output True Peak",
        "Output LRA",
        "Output Threshold",
        "Target Offset",
    ]:
        merged[key] = second.get(key)
    return (
        loudnorm_result_to_json(merged) +
        "\n\n--- loudnorm 1st pass raw log ---\n" +
        first_raw +
        "\n\n--- loudnorm 2nd pass raw log ---\n" +
        second_raw
    )

def collect_probes(files):
    cache = {}
    for p in files:
        try:
            cache[p] = probe_audio(p)
        except Exception as exc:
            cache[p] = {
                "duration": 0.0,
                "artist": "Unknown Artist",
                "album": "Unknown Album",
                "year": "Unknown Year",
                "title": os.path.splitext(os.path.basename(p))[0],
                "probe_error": str(exc),
            }
    return cache

def most_common_metadata_value(probes, key, default, unknown_values):
    counts = {}
    for probe in probes:
        value = probe.get(key)
        if not value or value in unknown_values:
            continue
        counts[value] = counts.get(value, 0) + 1

    if not counts:
        return default

    return max(counts.items(), key=lambda item: item[1])[0]

def choose_album_metadata(probe_cache):
    probes = list(probe_cache.values())
    artist = most_common_metadata_value(
        probes,
        "artist",
        "Unknown Artist",
        {"Unknown", "Unknown Artist"},
    )
    album = most_common_metadata_value(
        probes,
        "album",
        "Unknown Album",
        {"Unknown", "Unknown Album"},
    )
    year = most_common_metadata_value(
        probes,
        "year",
        "Unknown Year",
        {"Unknown", "Unknown Year"},
    )

    known_albums = {
        probe.get("album")
        for probe in probes
        if probe.get("album") not in (None, "", "Unknown", "Unknown Album")
    }
    if len(known_albums) > 1:
        album = "Mixed Selection"

    return artist, album, year

def tag_lookup(tags, *names, default=None):
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(k).lower()): v
        for k, v in tags.items()
    }
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        value = normalized.get(key)
        if value not in (None, ""):
            return str(value)
    return default

def safe_filename_part(value, fallback="Unknown"):
    text = str(value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_+", "_", text)
    return text.strip(" ._") or fallback

def export_timestamp():
    return datetime.now().strftime("%Y%m%d%H%M")

def format_duration(seconds):
    total_seconds = max(0, int(round(seconds or 0.0)))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"

def format_keep_datetime(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return dt.strftime("%Y%m%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "Unknown Date").replace("T", " ")[:19]

def load_app_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def save_app_settings(settings):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
     
def probe_audio(path):
    cmd = [
        FFPROBE_CMD,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        path
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(audio_tools_error_message([FFPROBE_CMD]))
    if proc.returncode != 0:
        raise RuntimeError(decode_ffmpeg_output(proc.stderr or b"ffprobe failed").strip())

    data = json.loads(proc.stdout.decode("utf-8", errors="ignore"))

    fmt = data.get("format", {})
    tags = fmt.get("tags", {})

    duration = 0.0
    try:
        duration = float(fmt.get("duration", 0.0))
    except Exception:
        pass

    album_artist = tag_lookup(tags, "album_artist", "albumartist", "album artist")
    artist = album_artist or tag_lookup(tags, "artist", "album artist", default="Unknown Artist")
    album = tag_lookup(tags, "album", default="Unknown Album")
    title = tag_lookup(tags, "title", default=os.path.splitext(os.path.basename(path))[0])
    date = tag_lookup(tags, "date", "year", default="Unknown Year")

    year = date[:4] if date[:4].isdigit() else date

    return {
        "duration": duration,
        "artist": artist,
        "album": album,
        "title": title,
        "year": year,
    }

def parse_loudnorm_output(text: str) -> dict:
    # ffmpeg logs may contain metadata with braces, so anchor to loudnorm JSON keys.
    match = re.search(r'\{\s*"input_i"\s*:', text)
    if not match:
        return {}

    start = match.start()
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, json.JSONDecodeError):
        return {}

    def f(key):
        v = data.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "Input Integrated": f("input_i"),
        "Input True Peak": f("input_tp"),
        "Input LRA": f("input_lra"),
        "Input Threshold": f("input_thresh"),
        "Output Integrated": f("output_i"),
        "Output True Peak": f("output_tp"),
        "Output LRA": f("output_lra"),
        "Output Threshold": f("output_thresh"),
        "Target Offset": f("target_offset"),
    }

def analyze_one_file(path, probe, two_pass, target):
    raw = run_loudnorm(path, two_pass, target)
    p = parse_loudnorm_output(raw)

    if not p or probe["duration"] <= 0:
        return ("invalid", None)

    input_lufs = p.get("Input Integrated")
    if input_lufs is None:
        return ("invalid", None)

    if input_lufs <= SILENCE_LUFS:
        return ("silent", None)

    required = [
        "Input Integrated",
        "Input True Peak",
        "Input LRA",
        "Input Threshold",
        "Output Integrated",
        "Output True Peak",
        "Output LRA",
        "Output Threshold",
    ]

    if any(p[k] is None for k in required):
        return ("invalid", None)

    return ("ok", {
        "name": os.path.basename(path),
        "duration": probe["duration"],
        "Input Integrated": p["Input Integrated"],
        "Input True Peak": p["Input True Peak"],
        "Input LRA": p["Input LRA"],
        "Input Threshold": p["Input Threshold"],
        "Output Integrated": p["Output Integrated"],
        "Output True Peak": p["Output True Peak"],
        "Output LRA": p["Output LRA"],
        "Output Threshold": p["Output Threshold"],
    })

def format_album_report_text(c):
    lines = []
    add = lines.append

    add(f"[Summary]\n※Based on theoretical values calculated from loudnorm {c['mode']} \n\n")
    add(
        f"{c['artist']} / {c['album']} ({c['year']})\n"
        f"Total Number of Tracks: {len(c['inputs_lra'])}\n"
        f"Skipped Tracks (analysis failure): {c['skipped']}\n"
        f"Skipped Tracks (silence): {c['silent']}\n"
        f"Total Running Time: {c['total_time']/60:.0f} min {c['total_time']%60:.0f} sec\n\n"
    )

    add(f"Avg Input LUFS (time-weighted): {c['avg_lufs_in']:.1f}\n")
    add(f"Min Input LUFS: {c['min_lufs_in']:.1f} ({c['min_lufs_in_track']})\n")
    add(f"Max Input LUFS: {c['max_lufs_in']:.1f} ({c['max_lufs_in_track']})\n\n")

    add(f"Album tracks with Input LRA < 4.0 LU : {len(c['low_lra_in_tracks'])}/{len(c['inputs_lra'])} ({c['lra_in_ratio']:.2f}%) \n")
    add(f"Avg Input LRA: {c['avg_lra_in']:.1f} LU\n")
    add(f"Min Input LRA: {c['min_lra_in']:.1f} LU ({c['min_lra_in_track']})\n")
    add(f"Max Input LRA: {c['max_lra_in']:.1f} LU ({c['max_lra_in_track']})\n\n")

    add(f"Avg Input True Peak: {c['avg_tp_in']:.1f} dBTP\n")
    add(f"Min Input True Peak: {c['min_tp_in']:.1f} dBTP ({c['min_tp_in_track']})\n")
    add(f"Max Input True Peak: {c['max_tp_in']:.1f} dBTP ({c['max_tp_in_track']})\n\n")

    add(f"Avg Input Thresholds (Loudness Gate): {c['avg_thr_in']:.1f}\n")
    add(f"Min Input Thresholds (Loudness Gate): {c['min_thr_in']:.1f} ({c['min_thr_in_track']})\n")
    add(f"Max Input Thresholds (Loudness Gate): {c['max_thr_in']:.1f} ({c['max_thr_in_track']})\n\n\n")

    add(
        f"[Outputs]\n"
        f"※Theoretical values after normalizing to {target_lufs(c['target']):g} LUFS target.\n"
        f"※Output values are loudnorm {c['mode']} estimates; real streaming behavior may differ slightly.\n\n"
    )
    add(f"Avg Output LUFS (time-weighted): {c['avg_lufs_out']:.1f}\n")
    add(f"Min Output LUFS: {c['min_lufs_out']:.1f} ({c['min_lufs_out_track']})\n")
    add(f"Max Output LUFS: {c['max_lufs_out']:.1f} ({c['max_lufs_out_track']})\n\n")

    add(f"Album tracks with Output LRA < 4.0 LU : {len(c['low_lra_out_tracks'])}/{len(c['outputs_lra'])} ({c['lra_out_ratio']:.2f}%) \n")
    add(f"Avg Output LRA: {c['avg_lra_out']:.1f} LU\n")
    add(f"Min Output LRA: {c['min_lra_out']:.1f} LU ({c['min_lra_out_track']})\n")
    add(f"Max Output LRA: {c['max_lra_out']:.1f} LU ({c['max_lra_out_track']})\n\n")

    add(f"Avg Output True Peak: {c['avg_tp_out']:.1f} dBTP\n")
    add(f"Min Output True Peak: {c['min_tp_out']:.1f} dBTP ({c['min_tp_out_track']})\n")
    add(f"Max Output True Peak: {c['max_tp_out']:.1f} dBTP ({c['max_tp_out_track']})\n\n")

    add(f"Avg Output Thresholds (Loudness Gate): {c['avg_thr_out']:.1f}\n")
    add(f"Min Output Thresholds (Loudness Gate): {c['min_thr_out']:.1f} ({c['min_thr_out_track']})\n")
    add(f"Max Output Thresholds (Loudness Gate): {c['max_thr_out']:.1f} ({c['max_thr_out_track']})\n\n\n")

    add("[Album Verdict]\n")
    add(f"  Verdict: {c['verdict']} ({c['verdict_label']})\n")
    add(f"  Listening Fatigue Score: {c['album_fatigue']:.1f} / 100\n\n")
    add("（◎=Comfortable / O=OK / △=Fatiguing / ×=Heavy Fatigue）\n\n")

    add("  Score Breakdown\n")
    add(f"    Core listening fatigue: {c['core_album_fatigue']:.1f} / 100\n")
    add(f"    High-risk time penalty: {c['high_risk_score']:.1f} / 100 (+{ALBUM_HIGH_RISK_WEIGHT * c['high_risk_score']:.1f})\n")
    add(f"    Flat time penalty: {c['flat_score']:.1f} / 100 (+{ALBUM_FLAT_WEIGHT * c['flat_score']:.1f})\n")
    add(f"    Input TP damage penalty: {c['tp_damage_score']:.1f} / 100 (+{ALBUM_TP_DAMAGE_WEIGHT * c['tp_damage_score']:.1f})\n")
    add(f"    Normalization penalty: {c['album_normalization_penalty']:.1f} / 100 (+{ALBUM_NORMALIZATION_WEIGHT * c['album_normalization_penalty']:.1f})\n\n")

    add("  Listening Time Flags\n")
    add(f"    High-risk time (core risk >= 65): {c['high_risk_time']/60:.1f} min / {c['total_time']/60:.1f} min ({c['high_risk_ratio'] * 100.0:.1f}%)\n")
    add(f"    Flat time (Output LRA < 3 LU): {c['flat_time']/60:.1f} min / {c['total_time']/60:.1f} min ({c['flat_ratio'] * 100.0:.1f}%)\n")
    add(f"    Input TP overs (> 0.0 dBTP): {len(c['clipped_tracks'])}/{len(c['results'])} tracks, {c['clipped_time']/60:.1f} min ({c['clipped_ratio'] * 100.0:.1f}%)\n\n")

    add("  Peak Tracks\n")
    add(f"    Peak core risk: {c['peak_risk']:.1f} / 100 ({c['peak_risk_track']})\n")
    add(f"    Peak normalization penalty: {c['peak_normalization_penalty']:.1f} / 100 ({c['peak_normalization_track']})\n")

    add("\n[Track Details]\n")
    add(
        "Each track shows LUFS / LRA / True Peak / Loudness Gate Threshold.\n"
    )
    for idx, r in enumerate(c["results"], 1):
        duration = r.get("duration", 0.0) or 0.0
        core_risk = track_fatigue_risk(r["Output LRA"], r["Output Threshold"])
        norm_penalty = normalization_penalty(
            r["Input Integrated"],
            r["Input True Peak"],
            c["target"],
        )
        add(f"\n{idx:02d}. {r['name']} ({format_duration(duration)})\n")
        add(
            f"    Input : LUFS {r['Input Integrated']:>5.1f} | "
            f"LRA {r['Input LRA']:>4.1f} LU | "
            f"TP {r['Input True Peak']:>5.1f} dBTP | "
            f"Thr {r['Input Threshold']:>5.1f}\n"
        )
        add(
            f"    Output: LUFS {r['Output Integrated']:>5.1f} | "
            f"LRA {r['Output LRA']:>4.1f} LU | "
            f"TP {r['Output True Peak']:>5.1f} dBTP | "
            f"Thr {r['Output Threshold']:>5.1f}\n"
        )
        add(
            f"    Risk  : Core {core_risk:>5.1f} / 100 | "
            f"Norm Penalty {norm_penalty:>5.1f} / 100\n"
        )
    add("\n")

    return "".join(lines)

class LufsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_app_settings()
        self.title("LUFS Inspector（ffmpeg loudnorm）")
        self.geometry(self.settings.get("window_geometry") or "1100x720")
        self.minsize(900, 560)
        self.settings_saved_on_destroy = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Destroy>", self.on_destroy, add="+")

        top = tk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(top, text="Select Folder", command=self.select_folder).pack(anchor="w")
        self.folder_label = tk.Label(top, text="Folder Not Selected")
        self.folder_label.pack(anchor="w", pady=(2, 0))
             
        main = tk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=10)

        left = tk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(left, text="Audio File").pack(anchor="w")

        self.listbox = tk.Listbox(
            left,
            width=40,
            selectmode=tk.EXTENDED
        )
        self.listbox.pack(fill=tk.Y, expand=True)
        self.listbox.bind("<Double-Button-1>", self.on_analyze_single)

        self.analyze_btn = tk.Button(left, text="Analyze Selected Track (Single)", command=self.on_analyze_single)
        self.analyze_btn.pack(fill=tk.X, pady=3)
        
        self.selected_btn = tk.Button(
            left,
            text="Analyze Selected Tracks (Multiple)",
            command=self.on_analyze_selected
        )
        self.selected_btn.pack(fill=tk.X, pady=3)
        
        self.album_btn = tk.Button(left, text="Analyze As an Album", command=self.on_analyze_album)
        self.album_btn.pack(fill=tk.X, pady=3)

        self.two_pass_var = tk.BooleanVar(value=True)
        self.two_pass_check = tk.Checkbutton(
            left,
            text="Use loudnorm 2-pass",
            variable=self.two_pass_var
        )
        self.two_pass_check.pack(anchor="w", pady=(8, 3))

        tk.Label(left, text="Targets").pack(anchor="w", pady=(12, 0))
        target_frame = tk.Frame(left)
        target_frame.pack(fill=tk.X)

        self.lufs_preset_var = tk.StringVar(value="streaming_14")
        self.custom_lufs_var = tk.DoubleVar(value=TARGET_LUFS)
        self.target_controls = []

        row = 0
        for key, label, _ in LUFS_PRESETS:
            radio = tk.Radiobutton(
                target_frame,
                text=label,
                variable=self.lufs_preset_var,
                value=key,
                command=self.update_lufs_custom_state,
            )
            radio.grid(row=row, column=0, columnspan=2, sticky="w")
            self.target_controls.append(radio)
            row += 1

        custom_radio = tk.Radiobutton(
            target_frame,
            text="Custom",
            variable=self.lufs_preset_var,
            value=CUSTOM_LUFS_PRESET,
            command=self.update_lufs_custom_state,
        )
        custom_radio.grid(row=row, column=0, sticky="w")
        self.target_controls.append(custom_radio)

        self.custom_lufs_spin = tk.Spinbox(
            target_frame,
            from_=-30.0,
            to=-5.0,
            increment=0.5,
            textvariable=self.custom_lufs_var,
            width=7,
        )
        self.custom_lufs_spin.grid(row=row, column=1, sticky="w", pady=1)
        self.target_controls.append(self.custom_lufs_spin)

        row += 1
        tk.Label(target_frame, text=f"TP: {TARGET_TP:g} dBTP (fixed)", anchor="w").grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1
        tk.Label(target_frame, text=f"LRA: {TARGET_LRA:g} LU (fixed)", anchor="w").grid(row=row, column=0, columnspan=2, sticky="w")
        self.update_lufs_custom_state()

        self.export_btn = tk.Button(left, text="Export Results", command=self.on_export_results, state=tk.DISABLED)
        self.export_btn.pack(fill=tk.X, pady=(12, 3))

        self.keep_btn = tk.Button(left, text="Keep Result", command=self.on_keep_result, state=tk.DISABLED)
        self.keep_btn.pack(fill=tk.X, pady=3)

        self.content_pane = tk.PanedWindow(
            main,
            orient=tk.HORIZONTAL,
            sashrelief=tk.RAISED,
            sashwidth=6,
        )
        self.content_pane.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(8, 0))

        right = tk.Frame(self.content_pane)
        self.keep_panel = tk.Frame(self.content_pane)
        self.content_pane.add(right, minsize=RESULTS_MIN_WIDTH)
        self.content_pane.add(self.keep_panel, minsize=KEEP_MIN_WIDTH, width=KEEP_DEFAULT_WIDTH)
        self.content_pane.bind("<ButtonRelease-1>", self.on_content_pane_changed)

        tk.Label(self.keep_panel, text="Kept Results").pack(anchor="w")

        self.delete_keep_btn = tk.Button(
            self.keep_panel,
            text="Delete Selected Result",
            command=self.on_delete_kept_result,
        )
        self.delete_keep_btn.pack(fill=tk.X, pady=(2, 4))

        sort_frame = tk.Frame(self.keep_panel)
        sort_frame.pack(fill=tk.X, pady=(0, 4))

        self.keep_sort_var = tk.StringVar(value=KEEP_SORT_FIELDS[0])
        self.keep_order_var = tk.StringVar(value=KEEP_SORT_ORDERS[0])

        sort_menu = tk.OptionMenu(
            sort_frame,
            self.keep_sort_var,
            *KEEP_SORT_FIELDS,
            command=lambda _: self.refresh_keep_listbox(),
        )
        sort_menu.config(width=16)
        sort_menu.pack(side=tk.LEFT, fill=tk.X, expand=True)

        order_menu = tk.OptionMenu(
            sort_frame,
            self.keep_order_var,
            *KEEP_SORT_ORDERS,
            command=lambda _: self.refresh_keep_listbox(),
        )
        order_menu.config(width=10)
        order_menu.pack(side=tk.RIGHT)

        keep_list_frame = tk.Frame(self.keep_panel)
        keep_list_frame.pack(fill=tk.BOTH, expand=True)

        self.keep_listbox = tk.Listbox(keep_list_frame, width=68)
        keep_y_scroll = tk.Scrollbar(keep_list_frame, orient=tk.VERTICAL, command=self.keep_listbox.yview)
        keep_x_scroll = tk.Scrollbar(keep_list_frame, orient=tk.HORIZONTAL, command=self.keep_listbox.xview)
        self.keep_listbox.configure(
            yscrollcommand=keep_y_scroll.set,
            xscrollcommand=keep_x_scroll.set,
        )
        self.keep_listbox.grid(row=0, column=0, sticky="nsew")
        keep_y_scroll.grid(row=0, column=1, sticky="ns")
        keep_x_scroll.grid(row=1, column=0, sticky="ew")
        keep_list_frame.grid_rowconfigure(0, weight=1)
        keep_list_frame.grid_columnconfigure(0, weight=1)
        self.keep_listbox.bind("<Double-Button-1>", self.on_load_kept_result)
        self.keep_listbox.bind("<Delete>", self.on_delete_kept_result)

        tk.Label(right, text="Results").pack(anchor="w")
        self.progress_var = tk.StringVar(value="Ready")
        tk.Label(right, textvariable=self.progress_var, anchor="w").pack(fill=tk.X)

        text_frame = tk.Frame(right)
        text_frame.pack(fill=tk.BOTH, expand=True)

        self.text = tk.Text(text_frame, wrap=tk.NONE)
        y_scroll = tk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.text.yview)
        x_scroll = tk.Scrollbar(text_frame, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        self.files = []
        self.current_folder = None
        self.last_export_payload = None
        self.analysis_started_at = None
        self.kept_results = []
        self.load_kept_results()
        self.after_idle(lambda: self.restore_content_pane_width(0))
        self.after(250, lambda: self.restore_content_pane_width(1))
        self.after(700, lambda: self.restore_content_pane_width(2))

    def restore_content_pane_width(self, attempt=0):
        keep_width = self.settings.get("keep_panel_width")
        keep_ratio = self.settings.get("keep_panel_ratio")
        if keep_width is None and keep_ratio is None:
            return

        try:
            pane_width = self.content_pane.winfo_width()
            if pane_width <= 1:
                if attempt < 8:
                    self.after(100, lambda: self.restore_content_pane_width(attempt + 1))
                return

            if keep_width is not None:
                keep_width = int(keep_width)
            else:
                keep_width = int(pane_width * float(keep_ratio))

            max_keep_width = max(KEEP_MIN_WIDTH, pane_width - RESULTS_MIN_WIDTH)
            keep_width = int(clamp(keep_width, KEEP_MIN_WIDTH, max_keep_width))
            self.content_pane.paneconfig(self.keep_panel, width=keep_width)
            self.content_pane.sash_place(0, pane_width - keep_width, 1)
        except Exception:
            pass

    def current_keep_panel_width(self):
        try:
            self.update_idletasks()
            keep_width = self.keep_panel.winfo_width()
            if keep_width > 1:
                return keep_width
        except Exception:
            pass

        try:
            sash_x, _ = self.content_pane.sash_coord(0)
            pane_width = self.content_pane.winfo_width()
            keep_width = pane_width - sash_x
            if keep_width > 0:
                return keep_width
        except Exception:
            pass

        return None

    def save_content_pane_width(self):
        keep_width = self.current_keep_panel_width()
        if keep_width is None:
            return

        pane_width = self.content_pane.winfo_width()
        self.settings["keep_panel_width"] = int(keep_width)
        if pane_width > 0:
            self.settings["keep_panel_ratio"] = keep_width / pane_width
        self.settings["window_geometry"] = self.geometry()
        save_app_settings(self.settings)

    def on_content_pane_changed(self, event=None):
        self.save_content_pane_width()

    def on_close(self):
        self.save_content_pane_width()
        self.settings_saved_on_destroy = True
        self.destroy()

    def on_destroy(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self.settings_saved_on_destroy:
            return
        self.save_content_pane_width()
        self.settings_saved_on_destroy = True

    def select_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        self.current_folder = folder
        self.folder_label.config(text=folder)
        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        self.files.clear()
        for name in sorted(os.listdir(self.current_folder)):
            full = os.path.join(self.current_folder, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                self.files.append(full)
                self.listbox.insert(tk.END, name)

    def update_lufs_custom_state(self, controls_enabled=True):
        if not hasattr(self, "custom_lufs_spin"):
            return
        if controls_enabled and self.lufs_preset_var.get() == CUSTOM_LUFS_PRESET:
            self.custom_lufs_spin.config(state=tk.NORMAL)
        else:
            self.custom_lufs_spin.config(state=tk.DISABLED)

    def selected_lufs_target(self):
        preset = self.lufs_preset_var.get()
        if preset == CUSTOM_LUFS_PRESET:
            return float(self.custom_lufs_var.get())

        for key, _, value in LUFS_PRESETS:
            if key == preset:
                return value

        return TARGET_LUFS

    def current_target_settings(self):
        preset = self.lufs_preset_var.get()
        preset_label = "Custom"
        lufs = self.selected_lufs_target()
        for key, label, _ in LUFS_PRESETS:
            if key == preset:
                preset_label = label
                break
        return {
            "preset": preset_label,
            "lufs": lufs,
            "tp": TARGET_TP,
            "lra": TARGET_LRA,
        }

    def update_targets_from_ui(self):
        try:
            return self.current_target_settings()
        except (tk.TclError, ValueError):
            messagebox.showerror("Invalid Target", "Please enter a numeric Custom LUFS target.")
            return None

    def ensure_audio_tools_available(self):
        missing = missing_audio_tools()
        if not missing:
            return True

        message = audio_tools_error_message(missing)
        self.progress_var.set(f"Missing: {', '.join(missing)}")
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, message + "\n")
        messagebox.showerror("FFmpeg Not Found", message)
        return False

    def set_controls_enabled(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.analyze_btn.config(state=state)
        self.selected_btn.config(state=state)
        self.album_btn.config(state=state)
        self.two_pass_check.config(state=state)
        for control in self.target_controls:
            control.config(state=state)
        self.update_lufs_custom_state(enabled)
        if enabled and self.last_export_payload is not None:
            self.export_btn.config(state=tk.NORMAL)
            self.keep_btn.config(state=tk.NORMAL)
        else:
            self.export_btn.config(state=tk.DISABLED)
            self.keep_btn.config(state=tk.DISABLED)

    def on_analyze_single(self, event=None):
        sel = self.listbox.curselection()
        if len(sel) != 1:
            messagebox.showinfo(
                "Info",
                "Please select exactly one file for single track analysis"
            )
            return
        self.run_single(self.files[sel[0]])

    def run_single(self, path):
        if not self.ensure_audio_tools_available():
            return

        target = self.update_targets_from_ui()
        if target is None:
            return
        two_pass = self.two_pass_var.get()
        self.text.delete("1.0", tk.END)
        self.progress_var.set("Analyzing single track...")
        self.last_export_payload = None
        self.export_btn.config(state=tk.DISABLED)
        self.keep_btn.config(state=tk.DISABLED)
        mode = "2-pass" if two_pass else "1-pass"
        self.text.insert(tk.END, f"Analyzing ({mode})...\n{path}\n")
        threading.Thread(target=self.worker_single, args=(path, two_pass, target), daemon=True).start()
        self.set_controls_enabled(False)

    def worker_single(self, path, two_pass, target):
        try:
            metadata = probe_audio(path)
        except Exception:
            metadata = {
                "artist": "Unknown Artist",
                "album": "Unknown Album",
                "title": os.path.splitext(os.path.basename(path))[0],
                "year": "Unknown Year",
            }
        try:
            raw = run_loudnorm(path, two_pass, target)
        except Exception as exc:
            self.after(0, lambda: self.show_analysis_error(str(exc)))
            return
        parsed = parse_loudnorm_output(raw)
        self.after(0, lambda: self.show_single(path, parsed, raw, two_pass, metadata, target))
    
    def on_analyze_selected(self):
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showinfo("Info", "Please select tracks")
            return

        selected_files = [self.files[i] for i in indices]
        self.analyze_files(selected_files, analysis_kind="selection")
    
    def analyze_files(self, files, analysis_kind="selection"):
        if not files:
            messagebox.showinfo("Info", "No tracks selected")
            return

        if not self.ensure_audio_tools_available():
            return

        target = self.update_targets_from_ui()
        if target is None:
            return

        self.last_export_payload = None
        self.analysis_started_at = time.time()
        self.set_controls_enabled(False)
        self.text.delete("1.0", tk.END)
        two_pass = self.two_pass_var.get()
        mode = "2-pass" if two_pass else "1-pass"
        label = "album" if analysis_kind == "album" else "selected tracks"
        self.progress_var.set(f"Progress: 0/{len(files)}")
        self.text.insert(tk.END, f"Analyzing {label} ({mode})...\n\n")
        threading.Thread(
            target=self.worker_album,
            args=(files, two_pass, target, analysis_kind),
            daemon=True
        ).start()
    
    def show_single(self, path, parsed, raw, two_pass, metadata, target):
        self.text.delete("1.0", tk.END)

        mode = "2-pass" if two_pass else "1-pass"
        self.text.insert(tk.END, f"File:\n{path}\n\n※Based on theoretical values calculated from loudnorm {mode} \n\n")

        if not parsed:
            self.text.insert(tk.END, "Failed to Analyze\n\n")
            self.text.insert(tk.END, raw)
            self.last_export_payload = {
                "type": "single",
                "file": path,
                "metadata": metadata,
                "mode": mode,
                "target": target,
                "error": "Failed to analyze",
                "raw": raw,
            }
            self.progress_var.set("Complete")
            self.set_controls_enabled(True)
            return

        input_lufs = parsed.get("Input Integrated")
        if input_lufs is not None:
            diff = input_lufs - target_lufs(target)
            self.text.insert(
                tk.END,
                f"▶ Input Integrated: {input_lufs:.1f} LUFS\n"
                f"  → Normalizes {-1 * diff:+.1f} dB when streamed\n\n"
            )

        self.text.insert(tk.END, "[Input]\n")
        for key in ["Input Integrated", "Input True Peak", "Input LRA", "Input Threshold"]:
            if key in parsed:
                self.text.insert(tk.END, f"  {key}: {parsed[key]}\n")

        self.text.insert(tk.END, f"\n[If normalized to {target_lufs(target):g} LUFS]\n")
        for key in ["Output Integrated", "Output True Peak", "Output LRA", "Output Threshold"]:
            if key in parsed:
                self.text.insert(tk.END, f"  {key}: {parsed[key]}\n")

        self.text.insert(tk.END, "\n--- raw loudnorm log ---\n")
        self.text.insert(tk.END, raw)
        self.last_export_payload = {
            "type": "single",
            "file": path,
            "metadata": metadata,
            "mode": mode,
            "target": target,
            "result": parsed,
            "raw": raw,
        }
        self.progress_var.set("Complete")
        self.set_controls_enabled(True)

    def show_analysis_error(self, message):
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, message + "\n")
        self.progress_var.set("Analysis failed")
        self.last_export_payload = None
        messagebox.showerror("Analysis Failed", message)
        self.set_controls_enabled(True)

    def on_analyze_album(self):
        self.analyze_files(self.files, analysis_kind="album")

    def update_progress(self, d, t, name=None, status=None):
        progress = f"Progress: {d}/{t}"
        if self.analysis_started_at and d > 0 and d < t:
            elapsed = time.time() - self.analysis_started_at
            remaining = elapsed / d * (t - d)
            progress += f" | ETA: {remaining/60:.1f} min"
        if name:
            progress += f" | Last: {name}"
        if status:
            progress += f" ({status})"

        self.progress_var.set(progress)

    def payload_display_parts(self, payload):
        metadata = payload.get("metadata") or {}
        summary = payload.get("summary") or {}
        artist = metadata.get("artist") or "Unknown Artist"

        if payload.get("type") == "single":
            title = (
                metadata.get("title")
                or os.path.splitext(os.path.basename(payload.get("file", "")))[0]
                or "Unknown Song"
            )
            return artist, title

        if payload.get("type") == "selection":
            track_count = summary.get("track_count") or len(payload.get("tracks") or [])
            album = metadata.get("album") or "Selected Tracks"
            return artist, f"{album} ({track_count} selected tracks)"

        album = metadata.get("album") or "Unknown Album"
        return artist, album

    def payload_keep_kind(self, payload):
        payload_type = payload.get("type")
        if payload_type == "single":
            return "SINGLE"
        if payload_type == "selection":
            return "TRACKS"
        return "ALBUM"

    def payload_fatigue_score(self, payload):
        score = (payload.get("summary") or {}).get("listening_fatigue_score")
        try:
            return float(score)
        except (TypeError, ValueError):
            return None

    def on_keep_result(self):
        if self.last_export_payload is None:
            messagebox.showinfo("Info", "No results to keep yet.")
            return

        kept_at = datetime.now()
        artist, title = self.payload_display_parts(self.last_export_payload)
        kind = self.payload_keep_kind(self.last_export_payload)
        score = self.payload_fatigue_score(self.last_export_payload)
        report_text = self.text.get("1.0", tk.END).rstrip() + "\n"

        record = {
            "kept_at": kept_at.isoformat(timespec="seconds"),
            "kept_at_display": format_keep_datetime(kept_at),
            "kind": kind,
            "artist": artist,
            "title": title,
            "listening_fatigue_score": score,
            "payload": self.last_export_payload,
            "report_text": report_text,
        }

        os.makedirs(KEEP_DIR, exist_ok=True)
        filename = "_".join(
            safe_filename_part(part)
            for part in (
                kept_at.strftime("%Y%m%d_%H%M%S_%f"),
                artist,
                title,
            )
        ) + ".json"
        path = os.path.join(KEEP_DIR, filename)

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("Keep Failed", str(exc))
            return

        self.load_kept_results(select_path=path)
        self.progress_var.set(f"Kept: {record['kept_at_display']} | {artist} / {title}")

    def load_kept_results(self, select_path=None):
        entries = []
        if os.path.isdir(KEEP_DIR):
            for name in os.listdir(KEEP_DIR):
                if not name.lower().endswith(".json"):
                    continue
                path = os.path.join(KEEP_DIR, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue

                entry = self.normalize_kept_record(path, data)
                if entry is not None:
                    entries.append(entry)

        self.kept_results = entries
        self.refresh_keep_listbox(select_path=select_path)

    def normalize_kept_record(self, path, data):
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None

        kept_at = data.get("kept_at")
        if not kept_at:
            kept_at = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")

        artist, title = self.payload_display_parts(payload)
        kind = data.get("kind") or self.payload_keep_kind(payload)
        score = data.get("listening_fatigue_score")
        if score is None:
            score = self.payload_fatigue_score(payload)
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None

        return {
            "path": path,
            "kept_at": kept_at,
            "kept_at_display": data.get("kept_at_display") or format_keep_datetime(kept_at),
            "kind": kind,
            "artist": data.get("artist") or artist,
            "title": data.get("title") or title,
            "track_count": (payload.get("summary") or {}).get("track_count") or len(payload.get("tracks") or []),
            "score": score,
        }

    def refresh_keep_listbox(self, select_path=None):
        if not hasattr(self, "keep_listbox"):
            return

        sort_by = self.keep_sort_var.get()
        reverse = self.keep_order_var.get() == "Descending"

        def sort_key(entry):
            if sort_by == "Artist":
                return (
                    str(entry.get("artist") or "").lower(),
                    str(entry.get("title") or "").lower(),
                    str(entry.get("kept_at") or ""),
                )
            if sort_by == "Ear Fatigue Score":
                score = entry.get("score")
                if score is None:
                    return float("-inf") if reverse else float("inf")
                return score
            return str(entry.get("kept_at") or "")

        self.kept_results.sort(key=sort_key, reverse=reverse)
        self.keep_listbox.delete(0, tk.END)
        selected_index = None

        for idx, entry in enumerate(self.kept_results):
            self.keep_listbox.insert(tk.END, self.keep_list_label(entry))
            if select_path and entry.get("path") == select_path:
                selected_index = idx

        if selected_index is not None:
            self.keep_listbox.selection_set(selected_index)
            self.keep_listbox.see(selected_index)

    def keep_list_label(self, entry):
        score = entry.get("score")
        score_text = f"{score:5.1f}" if score is not None else "  -- "
        kind = entry.get("kind") or "ALBUM"
        count = entry.get("track_count")
        count_text = f" {count}t" if count and kind != "SINGLE" else ""
        return (
            f"{entry.get('kept_at_display', 'Unknown Date')} | "
            f"{kind}{count_text} | "
            f"EFS {score_text} | "
            f"{entry.get('artist', 'Unknown Artist')} / {entry.get('title', 'Unknown')}"
        )

    def on_load_kept_result(self, event=None):
        selection = self.keep_listbox.curselection()
        if not selection:
            return

        entry = self.kept_results[selection[0]]
        try:
            with open(entry["path"], "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            messagebox.showerror("Load Failed", str(exc))
            return

        payload = data.get("payload")
        if not isinstance(payload, dict):
            messagebox.showerror("Load Failed", "Kept result is missing measurement data.")
            return

        report_text = data.get("report_text")
        if not report_text:
            report_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

        self.last_export_payload = payload
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, report_text.rstrip() + "\n")
        self.progress_var.set(f"Loaded result: {self.keep_list_label(entry)}")
        self.set_controls_enabled(True)

    def on_delete_kept_result(self, event=None):
        selection = self.keep_listbox.curselection()
        if not selection:
            messagebox.showinfo("Info", "Please select a result to delete.")
            return

        entry = self.kept_results[selection[0]]
        label = self.keep_list_label(entry)
        ok = messagebox.askyesno(
            "Delete Result",
            f"Delete this kept result?\n\n{label}",
        )
        if not ok:
            return

        try:
            os.remove(entry["path"])
        except FileNotFoundError:
            pass
        except Exception as exc:
            messagebox.showerror("Delete Failed", str(exc))
            return

        self.load_kept_results()
        self.progress_var.set(f"Deleted kept result: {label}")

    def default_export_filename(self, extension=".txt"):
        payload = self.last_export_payload or {}
        metadata = payload.get("metadata") or {}
        stamp = export_timestamp()

        if payload.get("type") == "single":
            artist = metadata.get("artist") or "Unknown Artist"
            title = (
                metadata.get("title")
                or os.path.splitext(os.path.basename(payload.get("file", "")))[0]
                or "Unknown Song"
            )
            parts = [artist, title, stamp]
        else:
            artist = metadata.get("artist") or "Unknown Artist"
            album = metadata.get("album") or "Unknown Album"
            parts = [artist, album, stamp]

        base = "_".join(safe_filename_part(part) for part in parts)
        return base + extension

    def export_initial_dir(self):
        payload = self.last_export_payload or {}
        file_path = payload.get("file")
        if file_path:
            return os.path.dirname(file_path)
        if self.current_folder:
            return self.current_folder
        return os.getcwd()

    def on_export_results(self):
        if self.last_export_payload is None:
            messagebox.showinfo("Info", "No results to export yet.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialdir=self.export_initial_dir(),
            initialfile=self.default_export_filename(".txt"),
            filetypes=[
                ("Text report", "*.txt"),
                ("JSON data", "*.json"),
                ("CSV data", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        if not ext:
            path += ".txt"
            ext = ".txt"
        try:
            if ext == ".json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.last_export_payload, f, ensure_ascii=False, indent=2)
            elif ext == ".csv":
                self.write_csv_export(path)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.text.get("1.0", tk.END).rstrip() + "\n")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc))
            return

        messagebox.showinfo("Export Complete", f"Saved:\n{path}")

    def write_csv_export(self, path):
        payload = self.last_export_payload or {}
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["section", "key", "value"])

            for key, value in payload.get("metadata", {}).items():
                writer.writerow(["metadata", key, value])
            for key, value in payload.get("target", {}).items():
                writer.writerow(["target", key, value])
            for key, value in payload.get("summary", {}).items():
                writer.writerow(["summary", key, value])

            tracks = payload.get("tracks") or []
            single = payload.get("result")
            if tracks:
                writer.writerow([])
                track_keys = [
                    "name",
                    "duration",
                    "Input Integrated",
                    "Input True Peak",
                    "Input LRA",
                    "Input Threshold",
                    "Output Integrated",
                    "Output True Peak",
                    "Output LRA",
                    "Output Threshold",
                ]
                writer.writerow(track_keys)
                for row in tracks:
                    writer.writerow([row.get(k, "") for k in track_keys])
            elif single:
                writer.writerow([])
                writer.writerow(["metric", "value"])
                for key, value in single.items():
                    writer.writerow([key, value])

    def worker_album(self, files, two_pass, target, analysis_kind):
        # --- phase 1: probe (serial) ---
        probe_cache = collect_probes(files)

        artist, album, year = choose_album_metadata(probe_cache)
        
        # --- phase 2: loudnorm (parallel) ---
        skipped = 0
        silent = 0
        results = []
        
        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as executor:
            futures = {
                executor.submit(analyze_one_file, p, probe_cache[p], two_pass, target): p 
                for p in files
            }
            
            total = len(files)
            done = 0
            for f in as_completed(futures):
                path = futures[f]
                try:
                    status, payload = f.result()
                except Exception:
                    status, payload = "invalid", None
                if status == "ok":
                    results.append(payload)
                elif status == "silent":
                    silent += 1
                else:
                    skipped += 1
                done += 1
                self.after(0, self.update_progress, done, total, os.path.basename(path), status)
            results.sort(key=lambda r: r["name"])
        self.after(0, lambda: self.show_album_summary(results, artist, album, year, skipped, silent, two_pass, target, analysis_kind))

    def show_album_summary(self, results, artist, album, year, skipped, silent, two_pass, target, analysis_kind):

        total_time = sum(r["duration"] for r in results if r["duration"] > 0)
        payload_type = "album" if analysis_kind == "album" else "selection"

        if total_time <= 0:
            self.text.delete("1.0", tk.END)
            self.text.insert(
                tk.END,
                "[Summary]\n\n"
                "Album Verdict: Not Available\n"
                "Reason: All analyzed tracks have zero or invalid duration.\n"
                f"Skipped Tracks (insufficient loudnorm data): {skipped}\n"
            )
            self.last_export_payload = {
                "type": payload_type,
                "metadata": {
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "analysis_kind": payload_type,
                },
                "target": target,
                "summary": {
                    "analysis_kind": payload_type,
                    "verdict": "Not Available",
                    "skipped_tracks": skipped,
                    "silent_tracks": silent,
                    "reason": "All analyzed tracks have zero or invalid duration.",
                },
                "tracks": results,
            }
            self.progress_var.set("Complete")
            self.set_controls_enabled(True)
            return

        # --- time-weighted only for Integrated LUFS ---
        avg_lufs_in  = time_weighted_avg(results, "Input Integrated")
        avg_lufs_out = time_weighted_avg(results, "Output Integrated")

        # --- rebuild OG-style structures ---
        inputs_lufs = [(r["Input Integrated"], r["name"]) for r in results]
        inputs_lra  = [(r["Input LRA"], r["name"]) for r in results]
        inputs_tp   = [(r["Input True Peak"], r["name"]) for r in results]
        inputs_thr  = [(r["Input Threshold"], r["name"]) for r in results]

        outputs_lufs = [(r["Output Integrated"], r["name"]) for r in results]
        outputs_lra  = [(r["Output LRA"], r["name"]) for r in results]
        outputs_tp   = [(r["Output True Peak"], r["name"]) for r in results]
        outputs_thr  = [(r["Output Threshold"], r["name"]) for r in results]

        # --- stats ---
        min_lufs_in, min_lufs_in_track = min(inputs_lufs, key=lambda x: x[0])
        max_lufs_in, max_lufs_in_track = max(inputs_lufs, key=lambda x: x[0])
        min_lufs_out, min_lufs_out_track = min(outputs_lufs, key=lambda x: x[0])
        max_lufs_out, max_lufs_out_track = max(outputs_lufs, key=lambda x: x[0])

        avg_lra_in = mean(v for v, _ in inputs_lra)
        min_lra_in, min_lra_in_track = min(inputs_lra, key=lambda x: x[0])
        max_lra_in, max_lra_in_track = max(inputs_lra, key=lambda x: x[0])
        low_lra_in_tracks = [v for v, _ in inputs_lra if v < 4.0]
        lra_in_ratio = len(low_lra_in_tracks) / len(inputs_lra) * 100

        avg_lra_out = mean(v for v, _ in outputs_lra)
        min_lra_out, min_lra_out_track = min(outputs_lra, key=lambda x: x[0])
        max_lra_out, max_lra_out_track = max(outputs_lra, key=lambda x: x[0])
        low_lra_out_tracks = [v for v, _ in outputs_lra if v < 4.0]
        lra_out_ratio = len(low_lra_out_tracks) / len(outputs_lra) * 100
        
        avg_tp_in = mean(v for v, _ in inputs_tp)
        min_tp_in, min_tp_in_track = min(inputs_tp, key=lambda x: x[0])
        max_tp_in, max_tp_in_track = max(inputs_tp, key=lambda x: x[0])

        avg_tp_out = mean(v for v, _ in outputs_tp)
        min_tp_out, min_tp_out_track = min(outputs_tp, key=lambda x: x[0])
        max_tp_out, max_tp_out_track = max(outputs_tp, key=lambda x: x[0])

        avg_thr_in = mean(v for v, _ in inputs_thr)
        min_thr_in, min_thr_in_track = min(inputs_thr, key=lambda x: x[0])
        max_thr_in, max_thr_in_track = max(inputs_thr, key=lambda x: x[0])

        avg_thr_out = mean(v for v, _ in outputs_thr)
        min_thr_out, min_thr_out_track = min(outputs_thr, key=lambda x: x[0])
        max_thr_out, max_thr_out_track = max(outputs_thr, key=lambda x: x[0])

        fatigue_metrics = compute_album_fatigue_metrics(results, total_time, max_tp_in, target)
        album_fatigue = fatigue_metrics["album_fatigue"]
        verdict = fatigue_metrics["verdict"]
        verdict_label = fatigue_metrics["verdict_label"]
        core_album_fatigue = fatigue_metrics["core_album_fatigue"]
        high_risk_time = fatigue_metrics["high_risk_time"]
        high_risk_ratio = fatigue_metrics["high_risk_ratio"]
        high_risk_score = fatigue_metrics["high_risk_score"]
        peak_risk = fatigue_metrics["peak_risk"]
        peak_risk_track = fatigue_metrics["peak_risk_track"]
        clipped_tracks = fatigue_metrics["clipped_tracks"]
        clipped_time = fatigue_metrics["clipped_time"]
        clipped_ratio = fatigue_metrics["clipped_ratio"]
        tp_damage_score = fatigue_metrics["tp_damage_score"]
        flat_time = fatigue_metrics["flat_time"]
        flat_ratio = fatigue_metrics["flat_ratio"]
        flat_score = fatigue_metrics["flat_score"]
        album_normalization_penalty = fatigue_metrics["album_normalization_penalty"]
        peak_normalization_penalty = fatigue_metrics["peak_normalization_penalty"]
        peak_normalization_track = fatigue_metrics["peak_normalization_track"]
        mode = "2-pass" if two_pass else "1-pass"
        self.last_export_payload = {
            "type": payload_type,
            "metadata": {
                "artist": artist,
                "album": album,
                "year": year,
                "mode": mode,
                "analysis_kind": payload_type,
            },
            "target": target,
            "summary": {
                "analysis_kind": payload_type,
                "track_count": len(results),
                "skipped_tracks": skipped,
                "silent_tracks": silent,
                "total_time_sec": total_time,
                "verdict": verdict,
                "verdict_label": verdict_label,
                "listening_fatigue_score": album_fatigue,
                "core_album_fatigue": core_album_fatigue,
                "high_risk_time_sec": high_risk_time,
                "high_risk_ratio": high_risk_ratio,
                "high_risk_score": high_risk_score,
                "flat_time_sec": flat_time,
                "flat_ratio": flat_ratio,
                "flat_score": flat_score,
                "input_tp_over_tracks": len(clipped_tracks),
                "input_tp_over_time_sec": clipped_time,
                "input_tp_over_ratio": clipped_ratio,
                "input_tp_damage_score": tp_damage_score,
                "normalization_penalty": album_normalization_penalty,
                "peak_core_risk": peak_risk,
                "peak_core_risk_track": peak_risk_track,
                "peak_normalization_penalty": peak_normalization_penalty,
                "peak_normalization_penalty_track": peak_normalization_track,
                "avg_input_lufs": avg_lufs_in,
                "avg_output_lufs": avg_lufs_out,
                "avg_input_lra": avg_lra_in,
                "avg_output_lra": avg_lra_out,
                "avg_input_true_peak": avg_tp_in,
                "avg_output_true_peak": avg_tp_out,
            },
            "tracks": results,
        }

        self.text.delete("1.0", tk.END)
        report_context = {
            "artist": artist,
            "album": album,
            "year": year,
            "skipped": skipped,
            "silent": silent,
            "mode": mode,
            "target": target,
            "results": results,
            "total_time": total_time,
            "inputs_lra": inputs_lra,
            "outputs_lra": outputs_lra,
            "avg_lufs_in": avg_lufs_in,
            "avg_lufs_out": avg_lufs_out,
            "min_lufs_in": min_lufs_in,
            "min_lufs_in_track": min_lufs_in_track,
            "max_lufs_in": max_lufs_in,
            "max_lufs_in_track": max_lufs_in_track,
            "min_lufs_out": min_lufs_out,
            "min_lufs_out_track": min_lufs_out_track,
            "max_lufs_out": max_lufs_out,
            "max_lufs_out_track": max_lufs_out_track,
            "avg_lra_in": avg_lra_in,
            "min_lra_in": min_lra_in,
            "min_lra_in_track": min_lra_in_track,
            "max_lra_in": max_lra_in,
            "max_lra_in_track": max_lra_in_track,
            "low_lra_in_tracks": low_lra_in_tracks,
            "lra_in_ratio": lra_in_ratio,
            "avg_lra_out": avg_lra_out,
            "min_lra_out": min_lra_out,
            "min_lra_out_track": min_lra_out_track,
            "max_lra_out": max_lra_out,
            "max_lra_out_track": max_lra_out_track,
            "low_lra_out_tracks": low_lra_out_tracks,
            "lra_out_ratio": lra_out_ratio,
            "avg_tp_in": avg_tp_in,
            "min_tp_in": min_tp_in,
            "min_tp_in_track": min_tp_in_track,
            "max_tp_in": max_tp_in,
            "max_tp_in_track": max_tp_in_track,
            "avg_tp_out": avg_tp_out,
            "min_tp_out": min_tp_out,
            "min_tp_out_track": min_tp_out_track,
            "max_tp_out": max_tp_out,
            "max_tp_out_track": max_tp_out_track,
            "avg_thr_in": avg_thr_in,
            "min_thr_in": min_thr_in,
            "min_thr_in_track": min_thr_in_track,
            "max_thr_in": max_thr_in,
            "max_thr_in_track": max_thr_in_track,
            "avg_thr_out": avg_thr_out,
            "min_thr_out": min_thr_out,
            "min_thr_out_track": min_thr_out_track,
            "max_thr_out": max_thr_out,
            "max_thr_out_track": max_thr_out_track,
            "album_fatigue": album_fatigue,
            "verdict": verdict,
            "verdict_label": verdict_label,
            "core_album_fatigue": core_album_fatigue,
            "high_risk_time": high_risk_time,
            "high_risk_ratio": high_risk_ratio,
            "high_risk_score": high_risk_score,
            "peak_risk": peak_risk,
            "peak_risk_track": peak_risk_track,
            "clipped_tracks": clipped_tracks,
            "clipped_time": clipped_time,
            "clipped_ratio": clipped_ratio,
            "tp_damage_score": tp_damage_score,
            "flat_time": flat_time,
            "flat_ratio": flat_ratio,
            "flat_score": flat_score,
            "album_normalization_penalty": album_normalization_penalty,
            "peak_normalization_penalty": peak_normalization_penalty,
            "peak_normalization_track": peak_normalization_track,
        }
        self.text.insert(tk.END, format_album_report_text(report_context))
        
        self.progress_var.set("Complete")
        self.set_controls_enabled(True)

if __name__ == "__main__":

    app = LufsApp()
    app.mainloop()
