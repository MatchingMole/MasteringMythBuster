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
import hashlib
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
ANALYSIS_MODE_INPUT = "input"
ANALYSIS_MODE_SPOTIFY = "spotify_normal"
ANALYSIS_MODE_EBU = "ebu_loudnorm"
SPOTIFY_NORMAL_LUFS = -14.0
SPOTIFY_HEADROOM_DB = -1.0
SILENCE_LUFS = -70.0   # anything <= this is treated as silence
FATIGUE_LRA_SAFE = 6.5
FATIGUE_LRA_HEAVY = 2.0
FATIGUE_THRESH_SAFE = -28.0
FATIGUE_THRESH_HEAVY = -20.0
FATIGUE_GATE_DEPTH_SAFE = 14.0
# BS.1770's relative gate is nominally 10 LU below the absolute-gated
# loudness. Treat that practical lower boundary as maximum density risk.
FATIGUE_GATE_DEPTH_HEAVY = 10.0
LUFS_EXPOSURE_SAFE = -14.0
LUFS_EXPOSURE_HEAVY = -8.0
PLR_SAFE = 12.0
PLR_HEAVY = 6.0
TRANSLATION_TURNDOWN_SAFE = 3.0
TRANSLATION_TURNDOWN_HEAVY = 8.0
EFS_VERSION = 7
EFD_VERSION = 1
ALBUM_CORE_WEIGHT = 0.50
ALBUM_HIGH_RISK_WEIGHT = 0.15
ALBUM_FLAT_WEIGHT = 0.10
ALBUM_TP_RISK_WEIGHT = 0.10
ALBUM_MODE_RISK_WEIGHT = 0.15
SPOTIFY_SOURCE_TP_WEIGHT = 0.10
SPOTIFY_PERSISTENT_WEIGHT = 0.15
SPOTIFY_PLR_BLEND = 0.60
SPOTIFY_TRANSLATION_BLEND = 0.40
APP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "LUFS Inspector",
)
KEEP_DIR = os.path.join(APP_DIR, "kept_results")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
KEEP_INDEX_PATH = os.path.join(APP_DIR, "kept_results_index.json")
CACHE_DIR = os.path.join(APP_DIR, "analysis_cache")
CACHE_VERSION = 1
ULTRA_CACHE_DIR = os.path.join(APP_DIR, "ultra_precision_cache")
ULTRA_CACHE_VERSION = 1
ULTRA_CONTEXT_SECONDS = 180.0
ULTRA_STEP_SECONDS = 30.0
KEEP_SORT_FIELDS = (
    "Measured Date",
    "Artist",
    "Ear Fatigue Score",
    "Ear Fatigue Dose",
)
KEEP_SORT_ORDERS = ("Descending", "Ascending")
KEEP_METHOD_FILTERS = ("ALL", "INPUT", "SPOTIFY", "EBU")
KEEP_KIND_FILTERS = ("ALL", "ALBUM", "TRACKS", "SINGLE")
KEEP_EFS_FILTERS = ("ALL", "COMFORTABLE", "OK", "FATIGUING", "HEAVY")
RESULTS_MIN_WIDTH = 320
KEEP_MIN_WIDTH = 360
KEEP_DEFAULT_WIDTH = 520

def natural_sort_key(value):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]

def analysis_cache_path(path, two_pass, target):
    stat = os.stat(path)
    identity = {
        "version": CACHE_VERSION,
        "path": os.path.normcase(os.path.abspath(path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "two_pass": bool(two_pass),
        "target": {
            "lufs": target_lufs(target),
            "tp": target_tp(target),
            "lra": target_lra(target),
        },
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".json")

def load_analysis_cache(path, two_pass, target):
    try:
        cache_path = analysis_cache_path(path, two_pass, target)
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("version") != CACHE_VERSION:
            return None
        raw = cached.get("raw")
        parsed = cached.get("parsed")
        if not isinstance(raw, str) or not isinstance(parsed, dict):
            return None
        return raw, parsed
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None

def save_analysis_cache(path, two_pass, target, raw, parsed):
    try:
        cache_path = analysis_cache_path(path, two_pass, target)
        os.makedirs(CACHE_DIR, exist_ok=True)
        temp_path = cache_path + f".{threading.get_ident()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"version": CACHE_VERSION, "raw": raw, "parsed": parsed},
                f,
                ensure_ascii=False,
            )
        os.replace(temp_path, cache_path)
    except (OSError, ValueError, TypeError):
        pass

def ultra_window_cache_path(path, context_start, context_duration, target):
    stat = os.stat(path)
    identity = {
        "version": ULTRA_CACHE_VERSION,
        "path": os.path.normcase(os.path.abspath(path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "context_start": round(float(context_start), 3),
        "context_duration": round(float(context_duration), 3),
        "target": {
            "lufs": target_lufs(target),
            "tp": target_tp(target),
            "lra": target_lra(target),
        },
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return os.path.join(ULTRA_CACHE_DIR, digest + ".json")

def load_ultra_window_cache(path, context_start, context_duration, target):
    try:
        cache_path = ultra_window_cache_path(
            path, context_start, context_duration, target
        )
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("version") != ULTRA_CACHE_VERSION:
            return None
        parsed = cached.get("parsed")
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None

def save_ultra_window_cache(path, context_start, context_duration, target, parsed):
    try:
        cache_path = ultra_window_cache_path(
            path, context_start, context_duration, target
        )
        os.makedirs(ULTRA_CACHE_DIR, exist_ok=True)
        temp_path = cache_path + f".{threading.get_ident()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                {"version": ULTRA_CACHE_VERSION, "parsed": parsed},
                f,
                ensure_ascii=False,
            )
        os.replace(temp_path, cache_path)
    except (OSError, ValueError, TypeError):
        pass

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

def analysis_mode(target):
    return (target or {}).get("analysis_mode", ANALYSIS_MODE_INPUT)

def analysis_mode_label(target):
    return {
        ANALYSIS_MODE_INPUT: "Input Master",
        ANALYSIS_MODE_SPOTIFY: "Spotify Normal (-14 LUFS)",
        ANALYSIS_MODE_EBU: "EBU R128 Loudnorm",
    }.get(analysis_mode(target), "Input Master")

def spotify_normal_gain(input_lufs, input_true_peak):
    desired_gain = SPOTIFY_NORMAL_LUFS - input_lufs
    if desired_gain <= 0.0:
        return desired_gain
    headroom_gain = SPOTIFY_HEADROOM_DB - input_true_peak
    return min(desired_gain, headroom_gain)

def apply_analysis_mode(parsed, target):
    result = dict(parsed or {})
    mode = analysis_mode(target)
    if not result or mode == ANALYSIS_MODE_EBU:
        return result

    input_lufs = result.get("Input Integrated")
    input_tp = result.get("Input True Peak")
    input_lra = result.get("Input LRA")
    input_threshold = result.get("Input Threshold")
    if None in (input_lufs, input_tp, input_lra, input_threshold):
        return result

    gain = 0.0
    if mode == ANALYSIS_MODE_SPOTIFY:
        gain = spotify_normal_gain(input_lufs, input_tp)

    result["Applied Gain"] = gain
    result["Output Integrated"] = input_lufs + gain
    result["Output True Peak"] = input_tp + gain
    result["Output LRA"] = input_lra
    result["Output Threshold"] = input_threshold + gain
    return result

def apply_spotify_album_preview(results):
    timed = [r for r in results if r.get("duration", 0.0) > 0]
    if not timed:
        return None, None
    total_time = sum(r["duration"] for r in timed)
    mean_energy = sum(
        (10.0 ** (r["Input Integrated"] / 10.0)) * r["duration"]
        for r in timed
    ) / total_time
    album_lufs = 10.0 * math.log10(mean_energy)
    max_tp = max(r["Input True Peak"] for r in timed)
    gain = spotify_normal_gain(album_lufs, max_tp)
    for r in timed:
        r["Applied Gain"] = gain
        r["Output Integrated"] = r["Input Integrated"] + gain
        r["Output True Peak"] = r["Input True Peak"] + gain
        r["Output LRA"] = r["Input LRA"]
        r["Output Threshold"] = r["Input Threshold"] + gain
    return album_lufs, gain

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

def lra_structural_risk(lra):
    return clamp(
        (FATIGUE_LRA_SAFE - lra) / (FATIGUE_LRA_SAFE - FATIGUE_LRA_HEAVY),
        0.0,
        1.0,
    )

def track_fatigue_risk(lra, threshold, integrated=None, relative_gate=False):
    lra_risk = lra_structural_risk(lra)
    if relative_gate and integrated is not None:
        gate_depth = integrated - threshold
        density_risk = clamp(
            (FATIGUE_GATE_DEPTH_SAFE - gate_depth)
            / (FATIGUE_GATE_DEPTH_SAFE - FATIGUE_GATE_DEPTH_HEAVY),
            0.0,
            1.0,
        )
    else:
        density_risk = clamp(
            (threshold - FATIGUE_THRESH_SAFE)
            / (FATIGUE_THRESH_HEAVY - FATIGUE_THRESH_SAFE),
            0.0,
            1.0,
        )
    combo_risk = lra_risk * density_risk
    fatigue = 100.0 * (
        0.65 * lra_risk +
        0.25 * density_risk +
        0.10 * combo_risk
    )
    return fatigue

def ratio_penalty(ratio):
    return 100.0 * math.sqrt(clamp(ratio, 0.0, 1.0))

def equivalent_fatigue_minutes(score, duration_seconds):
    """One EFM equals one minute of exposure at EFS 100."""
    try:
        score = float(score)
        duration_seconds = float(duration_seconds)
    except (TypeError, ValueError):
        return None
    if duration_seconds < 0.0:
        return None
    return clamp(score, 0.0, 100.0) / 100.0 * duration_seconds / 60.0

def true_peak_track_risk(true_peak):
    """Convex TP risk: minor inter-sample overs stay minor, +3 dBTP is severe."""
    if true_peak is None:
        return 0.0
    normalized_over = clamp(max(float(true_peak), 0.0) / 3.0, 0.0, 1.0)
    return 100.0 * normalized_over ** 2

def true_peak_risk_score(track_risks):
    """Combine typical and worst-track TP risk without inventing exposure time."""
    risks = [clamp(float(risk), 0.0, 100.0) for risk in track_risks]
    if not risks:
        return 0.0
    return 0.75 * mean(risks) + 0.25 * max(risks)

def lufs_exposure_penalty(integrated):
    if integrated is None:
        return 0.0
    return 100.0 * clamp(
        (integrated - LUFS_EXPOSURE_SAFE)
        / (LUFS_EXPOSURE_HEAVY - LUFS_EXPOSURE_SAFE),
        0.0,
        1.0,
    )

def plr_risk_score(integrated, true_peak):
    if integrated is None or true_peak is None:
        return 0.0
    plr = true_peak - integrated
    return 100.0 * clamp(
        (PLR_SAFE - plr) / (PLR_SAFE - PLR_HEAVY),
        0.0,
        1.0,
    )

def spotify_translation_risk(applied_gain, lra, integrated, true_peak):
    if applied_gain is None:
        return 0.0
    turn_down = max(0.0, -applied_gain)
    turn_down_risk = clamp(
        (turn_down - TRANSLATION_TURNDOWN_SAFE)
        / (TRANSLATION_TURNDOWN_HEAVY - TRANSLATION_TURNDOWN_SAFE),
        0.0,
        1.0,
    )
    persistent_density = max(
        lra_structural_risk(lra),
        plr_risk_score(integrated, true_peak) / 100.0,
    )
    return 100.0 * math.sqrt(turn_down_risk * persistent_density)

def album_fatigue_score(
    core_album_fatigue,
    high_risk_score,
    flat_score,
    tp_risk_score,
    mode_risk_score,
):
    return clamp(
        ALBUM_CORE_WEIGHT * core_album_fatigue +
        ALBUM_HIGH_RISK_WEIGHT * high_risk_score +
        ALBUM_FLAT_WEIGHT * flat_score +
        ALBUM_TP_RISK_WEIGHT * tp_risk_score +
        ALBUM_MODE_RISK_WEIGHT * mode_risk_score,
        0.0,
        100.0,
    )

def spotify_fatigue_score(
    core_album_fatigue,
    high_risk_score,
    flat_score,
    source_tp_damage_score,
    persistent_density_score,
):
    return clamp(
        ALBUM_CORE_WEIGHT * core_album_fatigue +
        ALBUM_HIGH_RISK_WEIGHT * high_risk_score +
        ALBUM_FLAT_WEIGHT * flat_score +
        SPOTIFY_SOURCE_TP_WEIGHT * source_tp_damage_score +
        SPOTIFY_PERSISTENT_WEIGHT * persistent_density_score,
        0.0,
        100.0,
    )

def normalization_penalty(input_integrated, input_true_peak, target=None):
    if input_integrated is None or input_true_peak is None:
        return 0.0

    down_gain = max(0.0, input_integrated - target_lufs(target))
    loudness_risk = clamp((down_gain - 3.0) / 7.0, 0.0, 1.0)
    peak_risk = clamp((input_true_peak + 1.0) / 1.0, 0.0, 1.0)
    return 100.0 * loudness_risk * peak_risk

def compute_album_fatigue_metrics(results, total_time, target=None):
    mode = analysis_mode(target)
    metric_prefix = "Input" if mode == ANALYSIS_MODE_INPUT else "Output"
    relative_gate = mode in (ANALYSIS_MODE_INPUT, ANALYSIS_MODE_SPOTIFY)
    lra_key = f"{metric_prefix} LRA"
    threshold_key = f"{metric_prefix} Threshold"
    integrated_key = f"{metric_prefix} Integrated"
    tp_key = f"{metric_prefix} True Peak"
    fatigue_tracks = [
        (
            0.0 if r.get("_silence") else track_fatigue_risk(
                r[lra_key],
                r[threshold_key],
                r[integrated_key],
                relative_gate,
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
        if r["duration"] > 0 and r[tp_key] > 0.0
    ]
    tp_track_risks = [
        (
            0.0 if r.get("_silence") else true_peak_track_risk(r[tp_key]),
            r[tp_key],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    mean_tp_track_risk = mean(risk for risk, _, _ in tp_track_risks)
    peak_tp_track_risk, peak_tp_value, peak_tp_risk_track = max(
        tp_track_risks, key=lambda item: item[0]
    )
    tp_risk_score = true_peak_risk_score(
        risk for risk, _, _ in tp_track_risks
    )

    source_tp_track_risks = [
        (
            0.0 if r.get("_silence") else true_peak_track_risk(r["Input True Peak"]),
            r["Input True Peak"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    source_tp_damage_score = true_peak_risk_score(
        risk for risk, _, _ in source_tp_track_risks
    )
    mean_source_tp_damage = mean(risk for risk, _, _ in source_tp_track_risks)
    peak_source_tp_damage, peak_source_tp_value, peak_source_tp_track = max(
        source_tp_track_risks, key=lambda item: item[0]
    )
    source_tp_over_tracks = [
        r for r in results
        if r["duration"] > 0 and r["Input True Peak"] > 0.0
    ]

    flat_tracks = [
        r for r in results
        if r["duration"] > 0 and not r.get("_silence") and r[lra_key] < 3.0
    ]
    flat_time = sum(r["duration"] for r in flat_tracks)
    flat_ratio = flat_time / total_time
    flat_score = ratio_penalty(flat_ratio)

    normalization_scored = mode == ANALYSIS_MODE_EBU
    normalization_penalties = [
        (
            normalization_penalty(r["Input Integrated"], r["Input True Peak"], target)
            if normalization_scored and not r.get("_silence") else 0.0,
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

    lufs_exposure_scores = [
        (
            lufs_exposure_penalty(r[integrated_key])
            if not normalization_scored and not r.get("_silence") else 0.0,
            r["duration"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    album_lufs_exposure_penalty = sum(
        penalty * duration
        for penalty, duration, _ in lufs_exposure_scores
    ) / total_time
    peak_lufs_exposure_penalty, _, peak_lufs_exposure_track = max(
        lufs_exposure_scores,
        key=lambda x: x[0],
    )
    mode_risk_score = (
        album_normalization_penalty
        if normalization_scored else album_lufs_exposure_penalty
    )
    mode_risk_kind = (
        "normalization_impact"
        if normalization_scored else f"{metric_prefix.lower()}_lufs_exposure"
    )

    plr_scores = [
        (
            0.0 if r.get("_silence") else plr_risk_score(
                r["Output Integrated"], r["Output True Peak"]
            ),
            r["duration"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    album_plr_risk = sum(
        risk * duration for risk, duration, _ in plr_scores
    ) / total_time
    peak_plr_risk, _, peak_plr_track = max(plr_scores, key=lambda x: x[0])

    translation_scores = [
        (
            spotify_translation_risk(
                r.get("Applied Gain"),
                r["Output LRA"],
                r["Output Integrated"],
                r["Output True Peak"],
            ) if mode == ANALYSIS_MODE_SPOTIFY and not r.get("_silence") else 0.0,
            r["duration"],
            r["name"],
        )
        for r in results
        if r["duration"] > 0
    ]
    album_translation_risk = sum(
        risk * duration for risk, duration, _ in translation_scores
    ) / total_time
    peak_translation_risk, _, peak_translation_track = max(
        translation_scores, key=lambda x: x[0]
    )
    spotify_persistent_risk = clamp(
        SPOTIFY_PLR_BLEND * album_plr_risk +
        SPOTIFY_TRANSLATION_BLEND * album_translation_risk,
        0.0,
        100.0,
    )

    if mode == ANALYSIS_MODE_SPOTIFY:
        mode_risk_score = spotify_persistent_risk
        mode_risk_kind = "spotify_persistent_density"
        album_fatigue = spotify_fatigue_score(
            core_album_fatigue,
            high_risk_score,
            flat_score,
            source_tp_damage_score,
            spotify_persistent_risk,
        )
        efs_weights = {
            "core": ALBUM_CORE_WEIGHT,
            "high_risk_time": ALBUM_HIGH_RISK_WEIGHT,
            "flat_time": ALBUM_FLAT_WEIGHT,
            "source_tp_damage_residue": SPOTIFY_SOURCE_TP_WEIGHT,
            "spotify_persistent_density": SPOTIFY_PERSISTENT_WEIGHT,
        }
    else:
        album_fatigue = album_fatigue_score(
            core_album_fatigue,
            high_risk_score,
            flat_score,
            tp_risk_score,
            mode_risk_score,
        )
        efs_weights = {
            "core": ALBUM_CORE_WEIGHT,
            "high_risk_time": ALBUM_HIGH_RISK_WEIGHT,
            "flat_time": ALBUM_FLAT_WEIGHT,
            "true_peak": ALBUM_TP_RISK_WEIGHT,
            "mode_risk": ALBUM_MODE_RISK_WEIGHT,
        }
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
        "mean_tp_track_risk": mean_tp_track_risk,
        "peak_tp_track_risk": peak_tp_track_risk,
        "peak_tp_value": peak_tp_value,
        "peak_tp_risk_track": peak_tp_risk_track,
        "tp_damage_score": tp_risk_score,
        "tp_risk_score": tp_risk_score,
        "source_tp_damage_score": source_tp_damage_score,
        "mean_source_tp_damage": mean_source_tp_damage,
        "peak_source_tp_damage": peak_source_tp_damage,
        "peak_source_tp_value": peak_source_tp_value,
        "peak_source_tp_track": peak_source_tp_track,
        "source_tp_over_tracks": source_tp_over_tracks,
        "flat_time": flat_time,
        "flat_ratio": flat_ratio,
        "flat_score": flat_score,
        "album_normalization_penalty": album_normalization_penalty,
        "peak_normalization_penalty": peak_normalization_penalty,
        "peak_normalization_track": peak_normalization_track,
        "normalization_scored": normalization_scored,
        "album_lufs_exposure_penalty": album_lufs_exposure_penalty,
        "peak_lufs_exposure_penalty": peak_lufs_exposure_penalty,
        "peak_lufs_exposure_track": peak_lufs_exposure_track,
        "mode_risk_score": mode_risk_score,
        "mode_risk_kind": mode_risk_kind,
        "album_plr_risk": album_plr_risk,
        "peak_plr_risk": peak_plr_risk,
        "peak_plr_track": peak_plr_track,
        "album_translation_risk": album_translation_risk,
        "spotify_persistent_risk": spotify_persistent_risk,
        "peak_translation_risk": peak_translation_risk,
        "peak_translation_track": peak_translation_track,
        "efs_weights": efs_weights,
        "metric_prefix": metric_prefix,
        "efs_version": EFS_VERSION,
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

def run_loudnorm_window(path, start, duration, target=None):
    loudnorm_args = (
        f"loudnorm=I={target_lufs(target):g}:TP={target_tp(target):g}:"
        f"LRA={target_lra(target):g}:print_format=json"
    )
    cmd = [
        FFMPEG_CMD,
        "-hide_banner",
        "-nostdin",
        "-ss", f"{max(0.0, float(start)):.3f}",
        "-t", f"{max(0.001, float(duration)):.3f}",
        "-i", path,
        "-vn",
        "-filter_complex", loudnorm_args,
        "-f", "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(audio_tools_error_message([FFMPEG_CMD]))
    return decode_ffmpeg_output(proc.stderr or b"")

def ultra_window_specs(path, duration):
    duration = max(0.0, float(duration or 0.0))
    if duration <= 0.0:
        return []
    context_duration = min(ULTRA_CONTEXT_SECONDS, duration)
    max_context_start = max(0.0, duration - context_duration)
    specs = []
    cell_start = 0.0
    while cell_start < duration - 1e-6:
        cell_duration = min(ULTRA_STEP_SECONDS, duration - cell_start)
        center = cell_start + cell_duration / 2.0
        context_start = clamp(
            center - context_duration / 2.0,
            0.0,
            max_context_start,
        )
        specs.append({
            "path": path,
            "cell_start": cell_start,
            "cell_duration": cell_duration,
            "context_start": context_start,
            "context_duration": context_duration,
        })
        cell_start += ULTRA_STEP_SECONDS
    return specs

def apply_shared_preview_gain(result, gain):
    result = dict(result)
    result["Applied Gain"] = gain
    result["Output Integrated"] = result["Input Integrated"] + gain
    result["Output True Peak"] = result["Input True Peak"] + gain
    result["Output LRA"] = result["Input LRA"]
    result["Output Threshold"] = result["Input Threshold"] + gain
    return result

def apply_precision_preview_gains(precision_results, track_results, album_shared=False):
    if not precision_results:
        return []
    if album_shared:
        gain = (track_results[0].get("Applied Gain", 0.0) or 0.0) if track_results else 0.0
        return [apply_shared_preview_gain(row, gain) for row in precision_results]

    gains_by_source = {
        row.get("name"): row.get("Applied Gain", 0.0) or 0.0
        for row in track_results
    }
    return [
        apply_shared_preview_gain(
            row,
            gains_by_source.get(row.get("source_name"), 0.0),
        )
        for row in precision_results
    ]

def analyze_ultra_window(spec, target):
    path = spec["path"]
    context_start = spec["context_start"]
    context_duration = spec["context_duration"]
    parsed = load_ultra_window_cache(
        path, context_start, context_duration, target
    )
    cache_hit = parsed is not None
    if parsed is None:
        raw = run_loudnorm_window(
            path, context_start, context_duration, target
        )
        parsed = parse_loudnorm_output(raw)
        if parsed:
            save_ultra_window_cache(
                path, context_start, context_duration, target, parsed
            )

    input_lufs = parsed.get("Input Integrated") if parsed else None
    is_silence = (
        input_lufs is None
        or not math.isfinite(input_lufs)
        or input_lufs <= SILENCE_LUFS
    )
    if is_silence:
        parsed = {
            "Input Integrated": SILENCE_LUFS,
            "Input True Peak": -99.0,
            "Input LRA": FATIGUE_LRA_SAFE,
            "Input Threshold": SILENCE_LUFS - FATIGUE_GATE_DEPTH_SAFE,
            "Output Integrated": SILENCE_LUFS,
            "Output True Peak": -99.0,
            "Output LRA": FATIGUE_LRA_SAFE,
            "Output Threshold": SILENCE_LUFS - FATIGUE_GATE_DEPTH_SAFE,
            "Target Offset": 0.0,
        }
    else:
        required = (
            "Input Integrated", "Input True Peak", "Input LRA", "Input Threshold"
        )
        if any(parsed.get(key) is None for key in required):
            return "invalid", None, cache_hit
        parsed = apply_analysis_mode(parsed, target)

    start = spec["cell_start"]
    end = start + spec["cell_duration"]
    name = (
        f"{os.path.basename(path)} @ "
        f"{format_duration(start)}-{format_duration(end)}"
    )
    row = {
        "name": name,
        "source_name": os.path.basename(path),
        "duration": spec["cell_duration"],
        "cell_start": start,
        "context_start": context_start,
        "context_duration": context_duration,
        "Input Integrated": parsed["Input Integrated"],
        "Input True Peak": parsed["Input True Peak"],
        "Input LRA": parsed["Input LRA"],
        "Input Threshold": parsed["Input Threshold"],
        "Output Integrated": parsed["Output Integrated"],
        "Output True Peak": parsed["Output True Peak"],
        "Output LRA": parsed["Output LRA"],
        "Output Threshold": parsed["Output Threshold"],
        "Applied Gain": parsed.get("Applied Gain"),
        "_silence": is_silence,
    }
    return "ok", row, cache_hit

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

def analyze_with_cache(path, two_pass=False, target=None):
    cached = load_analysis_cache(path, two_pass, target)
    if cached is not None:
        raw, parsed = cached
        return raw, parsed, True

    raw = run_loudnorm(path, two_pass, target)
    parsed = parse_loudnorm_output(raw)
    if parsed:
        save_analysis_cache(path, two_pass, target, raw, parsed)
    return raw, parsed, False

def collect_probes(files):
    cache = {}
    if not files:
        return cache

    def probe_one(p):
        try:
            return probe_audio(p)
        except Exception as exc:
            return {
                "duration": 0.0,
                "artist": "Unknown Artist",
                "album": "Unknown Album",
                "year": "Unknown Year",
                "title": os.path.splitext(os.path.basename(p))[0],
                "probe_error": str(exc),
            }

    max_workers = min(8, len(files), max(2, (os.cpu_count() or 2)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(probe_one, p): p for p in files}
        for future in as_completed(futures):
            cache[futures[future]] = future.result()
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

def format_duration_words(seconds):
    total_seconds = max(0, int(round(seconds or 0.0)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes} min {remaining_seconds} sec"

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

def load_keep_index():
    try:
        with open(KEEP_INDEX_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
    except Exception:
        pass
    return []

def save_keep_index(entries):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        temp_path = KEEP_INDEX_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, KEEP_INDEX_PATH)
    except Exception:
        pass

def keep_payload_fingerprint(payload):
    """Return a stable identity for one measured result, excluding raw log text."""
    def normalize(value, key=None):
        if isinstance(value, dict):
            return {
                item_key: normalize(item_value, item_key)
                for item_key, item_value in sorted(value.items())
                if item_key != "raw"
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if key == "file" and isinstance(value, str):
            return os.path.normcase(os.path.abspath(value))
        return value

    canonical = json.dumps(
        normalize(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def keep_payload_analysis_profile(payload):
    summary = payload.get("summary") or {}
    if (
        summary.get("fatigue_analysis") == "ultra_precision_windows"
        or payload.get("precision_windows")
    ):
        return "ULTRA"
    return "STANDARD"

def kept_entry_matches_payload(entry, payload):
    return (
        entry.get("duplicate_key") == keep_payload_fingerprint(payload)
        and entry.get("analysis_profile", "STANDARD")
        == keep_payload_analysis_profile(payload)
    )

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
    raw, p, cache_hit = analyze_with_cache(path, two_pass, target)
    p = apply_analysis_mode(p, target)

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
        "Applied Gain": p.get("Applied Gain"),
        "_cache_hit": cache_hit,
    })

def format_album_report_text(c):
    lines = []
    add = lines.append
    mode_kind = analysis_mode(c["target"])
    metric_prefix = "Input" if mode_kind == ANALYSIS_MODE_INPUT else "Output"
    relative_gate = mode_kind in (ANALYSIS_MODE_INPUT, ANALYSIS_MODE_SPOTIFY)
    core_basis = "LRA + relative gate depth" if relative_gate else "LRA + threshold"
    ultra_precision = bool(c.get("ultra_precision"))
    sample_label = "analysis windows" if ultra_precision else "tracks"
    sample_label_title = "Analysis Windows" if ultra_precision else "Tracks"

    add(
        f"[Summary]\n"
        f"Verdict / Preview Mode: {analysis_mode_label(c['target'])}\n"
        f"Measurement Pass: {c['mode']}\n\n"
    )
    if ultra_precision:
        add(
            f"Fatigue Analysis: Ultra-Precision "
            f"({ULTRA_CONTEXT_SECONDS:.0f}s context / {ULTRA_STEP_SECONDS:.0f}s step; "
            f"{c['precision_window_count']} windows)\n"
            "※EFS/time flags use fixed time windows; track summaries remain file-based.\n\n"
        )
    add(
        f"{c['artist']} / {c['album']} ({c['year']})\n"
        f"Total Number of Tracks: {len(c['inputs_lra'])}\n"
        f"Skipped Tracks (analysis failure): {c['skipped']}\n"
        f"Skipped Tracks (silence): {c['silent']}\n"
        f"Total Running Time: {format_duration_words(c['total_time'])}\n\n"
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

    if mode_kind != ANALYSIS_MODE_INPUT:
        if mode_kind == ANALYSIS_MODE_SPOTIFY:
            add(
                "[Spotify Normal Preview]\n"
                "※Gain-only -14 LUFS preview; LRA is not dynamically altered.\n"
                "※Album analysis uses one shared estimated album gain; selected tracks use track gain.\n\n"
                "※Spotify EFS scores gain-invariant PLR and persistent density after turn-down; "
                "Output TP/LUFS are informational.\n\n"
            )
            gains = [r.get("Applied Gain") for r in c["results"] if r.get("Applied Gain") is not None]
            if gains:
                add(f"Applied Gain: {min(gains):+.1f} to {max(gains):+.1f} dB\n\n")
        else:
            add(
                f"[EBU R128 Loudnorm Output]\n"
                f"※Theoretical values after normalizing to {target_lufs(c['target']):g} LUFS target.\n"
                f"※Output values are loudnorm {c['mode']} render estimates.\n\n"
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

    add(f"[{analysis_mode_label(c['target'])} Verdict]\n")
    add(f"  Verdict: {c['verdict']} ({c['verdict_label']})\n")
    add(f"  Listening Fatigue Score (EFS v{EFS_VERSION}): {c['album_fatigue']:.1f} / 100\n")
    add(f"  Ear Fatigue Dose (EFD v{EFD_VERSION}): {c['fatigue_dose_efm']:.1f} EFM\n")
    add("  ※1 EFM = one minute of exposure at EFS 100.\n\n")
    add("（◎=Comfortable / O=OK / △=Fatiguing / ×=Heavy Fatigue）\n\n")

    add("  Score Breakdown\n")
    add(f"    Core structural risk ({core_basis}): {c['core_album_fatigue']:.1f} / 100 (+{ALBUM_CORE_WEIGHT * c['core_album_fatigue']:.1f})\n")
    add(f"    High-risk time penalty: {c['high_risk_score']:.1f} / 100 (+{ALBUM_HIGH_RISK_WEIGHT * c['high_risk_score']:.1f})\n")
    add(f"    Flat time penalty: {c['flat_score']:.1f} / 100 (+{ALBUM_FLAT_WEIGHT * c['flat_score']:.1f})\n")
    if mode_kind == ANALYSIS_MODE_SPOTIFY:
        add(
            f"    Source TP damage residue: {c['source_tp_damage_score']:.1f} / 100 "
            f"(+{SPOTIFY_SOURCE_TP_WEIGHT * c['source_tp_damage_score']:.1f})\n"
        )
        add(
            f"    Persistent density risk (PLR 60% + Translation 40%): "
            f"{c['spotify_persistent_risk']:.1f} / 100 "
            f"(+{SPOTIFY_PERSISTENT_WEIGHT * c['spotify_persistent_risk']:.1f})\n"
        )
        add(f"      PLR / crest-factor component: {c['album_plr_risk']:.1f} / 100\n")
        add(f"      Normalization translation component: {c['album_translation_risk']:.1f} / 100\n\n")
    else:
        add(f"    {metric_prefix} TP risk: {c['tp_risk_score']:.1f} / 100 (+{ALBUM_TP_RISK_WEIGHT * c['tp_risk_score']:.1f})\n")
    if mode_kind == ANALYSIS_MODE_EBU:
        add(f"    Normalization impact: {c['album_normalization_penalty']:.1f} / 100 (+{ALBUM_MODE_RISK_WEIGHT * c['album_normalization_penalty']:.1f})\n\n")
    elif mode_kind == ANALYSIS_MODE_INPUT:
        add(f"    {metric_prefix} LUFS exposure: {c['album_lufs_exposure_penalty']:.1f} / 100 (+{ALBUM_MODE_RISK_WEIGHT * c['album_lufs_exposure_penalty']:.1f})\n\n")

    add("  Listening Time Flags\n")
    add(f"    High-risk time (core risk >= 65): {c['high_risk_time']/60:.1f} min / {c['total_time']/60:.1f} min ({c['high_risk_ratio'] * 100.0:.1f}%)\n")
    add(f"    Flat time ({metric_prefix} LRA < 3 LU): {c['flat_time']/60:.1f} min / {c['total_time']/60:.1f} min ({c['flat_ratio'] * 100.0:.1f}%)\n")
    if mode_kind == ANALYSIS_MODE_SPOTIFY:
        add(
            f"    Source {sample_label} with Input TP > 0.0 dBTP: "
            f"{len(c['source_tp_over_tracks'])}/{len(c['fatigue_results'])} {sample_label}\n"
        )
        add(
            f"    Source TP damage from per-{('window' if ultra_precision else 'track')} maxima: "
            f"mean {c['mean_source_tp_damage']:.1f} / 100, "
            f"peak {c['peak_source_tp_damage']:.1f} / 100 "
            f"({c['peak_source_tp_value']:.1f} dBTP, {c['peak_source_tp_track']})\n"
        )
        add(
            f"    {sample_label_title} with Output TP > 0.0 dBTP [informational; not scored]: "
            f"{len(c['clipped_tracks'])}/{len(c['fatigue_results'])} {sample_label}\n"
        )
    else:
        add(
            f"    {sample_label_title} with {metric_prefix} TP > 0.0 dBTP: "
            f"{len(c['clipped_tracks'])}/{len(c['fatigue_results'])} {sample_label}\n"
        )
        add(
            f"    TP risk from per-{('window' if ultra_precision else 'track')} maxima: mean {c['mean_tp_track_risk']:.1f} / 100, "
            f"peak {c['peak_tp_track_risk']:.1f} / 100 "
            f"({c['peak_tp_value']:.1f} dBTP, {c['peak_tp_risk_track']})\n"
        )
    add("\n")

    add(f"  Peak {sample_label_title}\n")
    add(f"    Peak core risk: {c['peak_risk']:.1f} / 100 ({c['peak_risk_track']})\n")
    if mode_kind == ANALYSIS_MODE_SPOTIFY:
        add(
            f"    Peak source TP damage: {c['peak_source_tp_damage']:.1f} / 100 "
            f"({c['peak_source_tp_track']})\n"
        )
        add(f"    Peak PLR risk: {c['peak_plr_risk']:.1f} / 100 ({c['peak_plr_track']})\n")
        add(f"    Peak translation risk: {c['peak_translation_risk']:.1f} / 100 ({c['peak_translation_track']})\n")
    elif c["normalization_scored"]:
        add(f"    Peak normalization impact: {c['peak_normalization_penalty']:.1f} / 100 ({c['peak_normalization_track']})\n")
    else:
        add(f"    Peak {metric_prefix} LUFS exposure: {c['peak_lufs_exposure_penalty']:.1f} / 100 ({c['peak_lufs_exposure_track']})\n")

    add("\n[Track Details]\n")
    add(
        "Each track shows LUFS / LRA / True Peak / Loudness Gate Threshold."
        + (" Track rows are informational; Ultra-Precision EFS uses the windows above.\n"
           if ultra_precision else "\n")
    )
    for idx, r in enumerate(c["results"], 1):
        duration = r.get("duration", 0.0) or 0.0
        core_risk = track_fatigue_risk(
            r[f"{metric_prefix} LRA"],
            r[f"{metric_prefix} Threshold"],
            r[f"{metric_prefix} Integrated"],
            relative_gate,
        )
        norm_penalty = normalization_penalty(
            r["Input Integrated"], r["Input True Peak"], c["target"]
        ) if c["normalization_scored"] else 0.0
        add(f"\n{idx:02d}. {r['name']} ({format_duration(duration)})\n")
        add(
            f"    Input : LUFS {r['Input Integrated']:>5.1f} | "
            f"LRA {r['Input LRA']:>4.1f} LU | "
            f"TP {r['Input True Peak']:>5.1f} dBTP | "
            f"Thr {r['Input Threshold']:>5.1f}\n"
        )
        if mode_kind != ANALYSIS_MODE_INPUT:
            add(
                f"    Output: LUFS {r['Output Integrated']:>5.1f} | "
                f"LRA {r['Output LRA']:>4.1f} LU | "
                f"TP {r['Output True Peak']:>5.1f} dBTP | "
                f"Thr {r['Output Threshold']:>5.1f}\n"
            )
        risk_line = f"    Risk  : Core {core_risk:>5.1f} / 100"
        if relative_gate:
            gate_depth = r[f"{metric_prefix} Integrated"] - r[f"{metric_prefix} Threshold"]
            risk_line += f" | Gate Depth {gate_depth:>4.1f} LU"
        if mode_kind == ANALYSIS_MODE_SPOTIFY:
            plr_risk = plr_risk_score(r["Output Integrated"], r["Output True Peak"])
            source_tp_risk = true_peak_track_risk(r["Input True Peak"])
            translation_risk = spotify_translation_risk(
                r.get("Applied Gain"),
                r["Output LRA"],
                r["Output Integrated"],
                r["Output True Peak"],
            )
            risk_line += (
                f" | Source TP {source_tp_risk:>5.1f} | "
                f"PLR {plr_risk:>5.1f} | Translation {translation_risk:>5.1f}"
            )
        elif c["normalization_scored"]:
            tp_track_risk = true_peak_track_risk(r[f"{metric_prefix} True Peak"])
            risk_line += f" | TP Risk {tp_track_risk:>5.1f} | Norm Impact {norm_penalty:>5.1f} / 100"
        else:
            lufs_penalty = lufs_exposure_penalty(r[f"{metric_prefix} Integrated"])
            tp_track_risk = true_peak_track_risk(r[f"{metric_prefix} True Peak"])
            risk_line += f" | TP Risk {tp_track_risk:>5.1f} | LUFS Exposure {lufs_penalty:>5.1f} / 100"
        add(risk_line + "\n")
    add("\n")

    return "".join(lines)

class LufsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_app_settings()
        self.title("LUFS Inspector（ffmpeg loudnorm）")
        self.geometry(self.settings.get("window_geometry") or "1100x720")
        self.minsize(900, 560)
        self.analysis_running = False
        self.settings_saved_on_destroy = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Destroy>", self.on_destroy, add="+")

        top = tk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)

        self.select_folder_btn = tk.Button(top, text="Select Folder", command=self.select_folder)
        self.select_folder_btn.pack(anchor="w")
        self.folder_label = tk.Label(top, text="Folder Not Selected")
        self.folder_label.pack(anchor="w", pady=(2, 0))
             
        main = tk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=10)

        left = tk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(left, text="Audio File").pack(anchor="w")

        audio_list_frame = tk.Frame(left)
        audio_list_frame.pack(fill=tk.BOTH, expand=True)
        self.listbox = tk.Listbox(
            audio_list_frame,
            width=40,
            selectmode=tk.EXTENDED,
            exportselection=False,
        )
        audio_scroll = tk.Scrollbar(audio_list_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=audio_scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        audio_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-Button-1>", self.on_analyze_single)
        self.listbox.bind("<<ListboxSelect>>", self.remember_audio_selection)

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

        self.ultra_precision_var = tk.BooleanVar(
            value=bool(self.settings.get("ultra_precision_mode", False))
        )
        self.ultra_precision_check = tk.Checkbutton(
            left,
            text="Ultra-Precision Mode (long-form / slow)",
            variable=self.ultra_precision_var,
        )
        self.ultra_precision_check.pack(anchor="w", pady=(2, 0))

        tk.Label(left, text="Verdict / Preview Mode").pack(anchor="w", pady=(8, 0))
        self.analysis_mode_var = tk.StringVar(value=ANALYSIS_MODE_INPUT)
        self.analysis_mode_controls = []
        for mode_value, mode_text in (
            (ANALYSIS_MODE_INPUT, "Input Master (default)"),
            (ANALYSIS_MODE_SPOTIFY, "Spotify Normal (-14 LUFS)"),
            (ANALYSIS_MODE_EBU, "EBU R128 Loudnorm"),
        ):
            radio = tk.Radiobutton(
                left,
                text=mode_text,
                variable=self.analysis_mode_var,
                value=mode_value,
                command=self.on_analysis_mode_changed,
            )
            radio.pack(anchor="w")
            self.analysis_mode_controls.append(radio)

        self.two_pass_var = tk.BooleanVar(value=True)
        self.ebu_options_frame = tk.LabelFrame(left, text="EBU R128 Settings")
        self.two_pass_check = tk.Checkbutton(
            self.ebu_options_frame,
            text="Use loudnorm 2-pass",
            variable=self.two_pass_var
        )
        self.two_pass_check.pack(anchor="w")

        target_frame = tk.Frame(self.ebu_options_frame)
        target_frame.pack(fill=tk.X, padx=4, pady=(0, 3))

        self.lufs_preset_values = {
            label: value for _, label, value in LUFS_PRESETS
        }
        self.lufs_preset_values["Custom"] = None
        self.lufs_preset_var = tk.StringVar(value=LUFS_PRESETS[0][1])
        self.custom_lufs_var = tk.DoubleVar(value=TARGET_LUFS)
        self.target_controls = []

        tk.Label(target_frame, text="Target:").grid(row=0, column=0, sticky="w")
        self.lufs_preset_menu = tk.OptionMenu(
            target_frame,
            self.lufs_preset_var,
            *self.lufs_preset_values.keys(),
            command=lambda _value: self.update_analysis_mode_state(),
        )
        self.lufs_preset_menu.config(anchor="w")
        self.lufs_preset_menu.grid(row=0, column=1, sticky="ew")
        self.target_controls.append(self.lufs_preset_menu)
        target_frame.grid_columnconfigure(1, weight=1)

        tk.Label(target_frame, text="Custom:").grid(row=1, column=0, sticky="w")
        self.custom_lufs_spin = tk.Spinbox(
            target_frame,
            from_=-30.0,
            to=-5.0,
            increment=0.5,
            textvariable=self.custom_lufs_var,
            width=7,
        )
        self.custom_lufs_spin.grid(row=1, column=1, sticky="w", pady=1)
        self.target_controls.append(self.custom_lufs_spin)

        tk.Label(
            target_frame,
            text=f"TP {TARGET_TP:g} dBTP / LRA {TARGET_LRA:g} LU (fixed)",
            anchor="w",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.export_btn = tk.Button(left, text="Export Results", command=self.on_export_results, state=tk.DISABLED)
        self.export_btn.pack(fill=tk.X, pady=(12, 3))

        self.keep_btn = tk.Button(left, text="Keep Result", command=self.on_keep_result, state=tk.DISABLED)
        self.keep_btn.pack(fill=tk.X, pady=3)
        self.update_analysis_mode_state()

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

        sort_frame = tk.Frame(self.keep_panel)
        sort_frame.pack(fill=tk.X, pady=(2, 4))

        self.keep_sort_var = tk.StringVar(value=KEEP_SORT_FIELDS[0])
        self.keep_order_var = tk.StringVar(value=KEEP_SORT_ORDERS[0])

        self.sort_button = tk.Button(
            sort_frame,
            textvariable=self.keep_sort_var,
            command=self.cycle_keep_sort,
        )
        self.sort_button.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.order_button = tk.Button(
            sort_frame,
            textvariable=self.keep_order_var,
            command=self.toggle_keep_order,
            width=12,
        )
        self.order_button.pack(side=tk.RIGHT, padx=(6, 0))

        filter_frame = tk.Frame(self.keep_panel)
        filter_frame.pack(fill=tk.X, pady=(0, 4))

        self.keep_method_filter_var = tk.StringVar(value=KEEP_METHOD_FILTERS[0])
        self.keep_kind_filter_var = tk.StringVar(value=KEEP_KIND_FILTERS[0])
        self.keep_efs_filter_var = tk.StringVar(value=KEEP_EFS_FILTERS[0])

        self.keep_method_filter_btn = tk.Button(
            filter_frame,
            text="Method: All",
            command=self.cycle_keep_method_filter,
        )
        self.keep_method_filter_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.keep_kind_filter_btn = tk.Button(
            filter_frame,
            text="Type: All",
            command=self.cycle_keep_kind_filter,
        )
        self.keep_kind_filter_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        self.keep_efs_filter_btn = tk.Button(
            filter_frame,
            text="EFS: All",
            command=self.cycle_keep_efs_filter,
        )
        self.keep_efs_filter_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        search_frame = tk.Frame(self.keep_panel)
        search_frame.pack(fill=tk.X, pady=(0, 4))
        tk.Label(search_frame, text="Find:").pack(side=tk.LEFT)
        self.keep_search_var = tk.StringVar(value="")
        self.keep_search_entry = tk.Entry(search_frame, textvariable=self.keep_search_var)
        self.keep_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        self.keep_search_var.trace_add("write", self.on_keep_search_changed)
        self.keep_clear_filter_btn = tk.Button(
            search_frame,
            text="Clear",
            command=self.clear_keep_filters,
            width=7,
        )
        self.keep_clear_filter_btn.pack(side=tk.RIGHT)

        self.keep_filter_status_var = tk.StringVar(value="Showing 0 / 0")
        tk.Label(
            self.keep_panel,
            textvariable=self.keep_filter_status_var,
            anchor="e",
        ).pack(fill=tk.X, pady=(0, 2))

        keep_list_frame = tk.Frame(self.keep_panel)
        keep_list_frame.pack(fill=tk.BOTH, expand=True)

        self.keep_listbox = tk.Listbox(
            keep_list_frame,
            width=68,
            exportselection=False,
            font="TkFixedFont",
        )
        keep_y_scroll = tk.Scrollbar(keep_list_frame, orient=tk.VERTICAL, command=self.keep_listbox.yview)
        keep_x_scroll = tk.Scrollbar(keep_list_frame, orient=tk.HORIZONTAL, command=self.keep_listbox.xview)
        self.keep_listbox.configure(yscrollcommand=keep_y_scroll.set, xscrollcommand=keep_x_scroll.set)
        self.keep_listbox.grid(row=0, column=0, sticky="nsew")
        keep_y_scroll.grid(row=0, column=1, sticky="ns")
        keep_x_scroll.grid(row=1, column=0, sticky="ew")
        keep_list_frame.grid_rowconfigure(0, weight=1)
        keep_list_frame.grid_columnconfigure(0, weight=1)
        self.keep_listbox.bind("<Double-Button-1>", self.on_load_kept_result)
        self.keep_listbox.bind("<Return>", self.on_load_kept_result)
        self.keep_listbox.bind("<Delete>", self.on_delete_kept_result)

        self.delete_keep_btn = tk.Button(
            self.keep_panel,
            text="Delete Selected Kept Result",
            command=self.on_delete_kept_result,
        )
        self.delete_keep_btn.pack(fill=tk.X, pady=(4, 0))

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
        self.selected_audio_paths = ()
        self.current_folder = None
        self.last_export_payload = None
        # Raw measurement values for the currently displayed analysis.  INPUT
        # and Spotify previews can be rebuilt from these without running
        # FFmpeg again.
        self.current_analysis_snapshot = None
        self.analysis_started_at = None
        self.kept_results = []
        self.visible_kept_results = []
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
        if hasattr(self, "ultra_precision_var"):
            self.settings["ultra_precision_mode"] = bool(
                self.ultra_precision_var.get()
            )
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
        self.selected_audio_paths = ()
        self.current_analysis_snapshot = None
        try:
            entries = [
                entry
                for entry in os.scandir(self.current_folder)
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in AUDIO_EXTS
            ]
        except OSError as exc:
            messagebox.showerror("Folder Error", str(exc))
            return
        for entry in sorted(entries, key=lambda item: natural_sort_key(item.name)):
            self.files.append(entry.path)
            self.listbox.insert(tk.END, entry.name)

    def remember_audio_selection(self, event=None):
        indices = self.listbox.curselection()
        self.selected_audio_paths = tuple(
            self.files[i]
            for i in indices
            if 0 <= i < len(self.files)
        )

    def update_analysis_mode_state(self, controls_enabled=True):
        if not hasattr(self, "custom_lufs_spin"):
            return
        ebu_selected = self.analysis_mode_var.get() == ANALYSIS_MODE_EBU
        if ebu_selected:
            if not self.ebu_options_frame.winfo_manager():
                self.ebu_options_frame.pack(
                    fill=tk.X,
                    pady=(8, 3),
                    before=self.export_btn,
                )
        else:
            self.ebu_options_frame.pack_forget()

        ebu_enabled = controls_enabled and ebu_selected
        self.two_pass_check.config(state=tk.NORMAL if ebu_enabled else tk.DISABLED)
        for control in self.target_controls:
            control.config(state=tk.NORMAL if ebu_enabled else tk.DISABLED)
        custom_enabled = ebu_enabled and self.lufs_preset_var.get() == "Custom"
        self.custom_lufs_spin.config(state=tk.NORMAL if custom_enabled else tk.DISABLED)
        ultra_enabled = controls_enabled and not ebu_selected
        if hasattr(self, "ultra_precision_check"):
            self.ultra_precision_check.config(
                state=tk.NORMAL if ultra_enabled else tk.DISABLED
            )

    def on_analysis_mode_changed(self):
        """Refresh a completed INPUT/Spotify result from its measured values."""
        self.update_analysis_mode_state(not getattr(self, "analysis_running", False))
        if getattr(self, "analysis_running", False):
            return

        snapshot = getattr(self, "current_analysis_snapshot", None)
        if not snapshot:
            return

        target = self.update_targets_from_ui()
        if target is None:
            return
        selected_mode = analysis_mode(target)

        # EBU output is an actual loudnorm render estimate and cannot be
        # reconstructed from INPUT measurements.  It is only reusable when
        # the current snapshot itself came from the same EBU settings.
        if selected_mode == ANALYSIS_MODE_EBU:
            source_target = snapshot.get("target") or {}
            same_ebu_settings = (
                analysis_mode(source_target) == ANALYSIS_MODE_EBU
                and bool(snapshot.get("two_pass")) == bool(self.two_pass_var.get())
                and target_lufs(source_target) == target_lufs(target)
                and target_tp(source_target) == target_tp(target)
                and target_lra(source_target) == target_lra(target)
            )
            if not same_ebu_settings:
                self.progress_var.set(
                    "EBU loudnorm output is not in the current result; press Analyze to render it."
                )
                # The report still shows the last valid mode, so prevent it
                # from being exported/kept under the newly selected EBU UI.
                self.export_btn.config(state=tk.DISABLED)
                self.keep_btn.config(state=tk.DISABLED)
                return

        if snapshot["type"] == "single":
            parsed = dict(snapshot["parsed"])
            if selected_mode != ANALYSIS_MODE_EBU:
                parsed = apply_analysis_mode(parsed, target)
            self.show_single(
                snapshot["path"], parsed, snapshot["raw"],
                snapshot["two_pass"] if selected_mode == ANALYSIS_MODE_EBU else False,
                snapshot["metadata"], target, cache_hit=True,
                remember_snapshot=False,
            )
            self.progress_var.set("Recalculated from current measurement")
            return

        results = [dict(row) for row in snapshot["results"]]
        precision_results = (
            [dict(row) for row in snapshot.get("precision_results") or []]
            if snapshot.get("ultra_precision") else None
        )
        if selected_mode != ANALYSIS_MODE_EBU:
            results = [apply_analysis_mode(row, target) for row in results]
            if precision_results is not None:
                precision_results = [
                    apply_analysis_mode(row, target) for row in precision_results
                ]
            if selected_mode == ANALYSIS_MODE_SPOTIFY and snapshot["analysis_kind"] == "album":
                apply_spotify_album_preview(results)
            if selected_mode == ANALYSIS_MODE_SPOTIFY and precision_results is not None:
                precision_results = apply_precision_preview_gains(
                    precision_results,
                    results,
                    album_shared=snapshot["analysis_kind"] == "album",
                )
        auto_keep_status = self.show_album_summary(
            results,
            snapshot["artist"], snapshot["album"], snapshot["year"],
            snapshot["skipped"], snapshot["silent"],
            snapshot["two_pass"] if selected_mode == ANALYSIS_MODE_EBU else False,
            target, snapshot["analysis_kind"], snapshot["cached_count"],
            remember_snapshot=False,
            precision_results=precision_results,
            ultra_precision=bool(snapshot.get("ultra_precision")),
            precision_cached_count=snapshot.get("precision_cached_count", 0),
            single_file=snapshot.get("single_file"),
            single_title=snapshot.get("single_title"),
        )
        status = "Recalculated from current measurement"
        if auto_keep_status == "kept":
            status += " | auto-kept"
        elif auto_keep_status == "duplicate":
            status += " | already kept"
        elif auto_keep_status == "error":
            status += " | auto-keep failed"
        self.progress_var.set(status)

    def selected_lufs_target(self):
        preset = self.lufs_preset_var.get()
        if preset == "Custom":
            return float(self.custom_lufs_var.get())
        return self.lufs_preset_values.get(preset, TARGET_LUFS)

    def current_target_settings(self):
        mode = self.analysis_mode_var.get()
        preset = self.lufs_preset_var.get()
        preset_label = preset
        lufs = self.selected_lufs_target() if mode == ANALYSIS_MODE_EBU else SPOTIFY_NORMAL_LUFS
        if mode == ANALYSIS_MODE_INPUT:
            preset_label = "Input measurement"
        elif mode == ANALYSIS_MODE_SPOTIFY:
            preset_label = "Spotify Normal (-14 LUFS)"
        return {
            "analysis_mode": mode,
            "analysis_mode_label": analysis_mode_label({"analysis_mode": mode}),
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
        self.analysis_running = not enabled
        self.select_folder_btn.config(state=state)
        self.listbox.config(state=state)
        self.analyze_btn.config(state=state)
        self.selected_btn.config(state=state)
        self.album_btn.config(state=state)
        self.ultra_precision_check.config(state=state)
        for control in self.analysis_mode_controls:
            control.config(state=state)
        self.update_analysis_mode_state(enabled)
        self.delete_keep_btn.config(state=state)
        self.keep_listbox.config(state=state)
        self.sort_button.config(state=state)
        self.order_button.config(state=state)
        self.keep_method_filter_btn.config(state=state)
        self.keep_kind_filter_btn.config(state=state)
        self.keep_efs_filter_btn.config(state=state)
        self.keep_search_entry.config(state=state)
        self.keep_clear_filter_btn.config(state=state)
        if enabled and self.last_export_payload is not None:
            self.export_btn.config(state=tk.NORMAL)
            self.keep_btn.config(state=tk.NORMAL)
        else:
            self.export_btn.config(state=tk.DISABLED)
            self.keep_btn.config(state=tk.DISABLED)

    def cycle_keep_sort(self):
        current = self.keep_sort_var.get()
        try:
            index = KEEP_SORT_FIELDS.index(current)
        except ValueError:
            index = -1
        self.keep_sort_var.set(KEEP_SORT_FIELDS[(index + 1) % len(KEEP_SORT_FIELDS)])
        self.refresh_keep_listbox()

    def toggle_keep_order(self):
        current = self.keep_order_var.get()
        self.keep_order_var.set(
            "Ascending" if current == "Descending" else "Descending"
        )
        self.refresh_keep_listbox()

    @staticmethod
    def _next_keep_filter(current, choices):
        try:
            index = choices.index(current)
        except ValueError:
            index = -1
        return choices[(index + 1) % len(choices)]

    def cycle_keep_method_filter(self):
        value = self._next_keep_filter(
            self.keep_method_filter_var.get(), KEEP_METHOD_FILTERS
        )
        self.keep_method_filter_var.set(value)
        self.keep_method_filter_btn.config(
            text=f"Method: {'All' if value == 'ALL' else value}"
        )
        self.refresh_keep_listbox()

    def cycle_keep_kind_filter(self):
        value = self._next_keep_filter(
            self.keep_kind_filter_var.get(), KEEP_KIND_FILTERS
        )
        self.keep_kind_filter_var.set(value)
        self.keep_kind_filter_btn.config(
            text=f"Type: {'All' if value == 'ALL' else value}"
        )
        self.refresh_keep_listbox()

    def cycle_keep_efs_filter(self):
        value = self._next_keep_filter(
            self.keep_efs_filter_var.get(), KEEP_EFS_FILTERS
        )
        self.keep_efs_filter_var.set(value)
        labels = {
            "ALL": "All",
            "COMFORTABLE": "< 25",
            "OK": "25 - 44.9",
            "FATIGUING": "45 - 64.9",
            "HEAVY": "65+",
        }
        self.keep_efs_filter_btn.config(text=f"EFS: {labels[value]}")
        self.refresh_keep_listbox()

    def on_keep_search_changed(self, *_args):
        self.refresh_keep_listbox()

    def clear_keep_filters(self):
        self.keep_method_filter_var.set("ALL")
        self.keep_kind_filter_var.set("ALL")
        self.keep_efs_filter_var.set("ALL")
        self.keep_method_filter_btn.config(text="Method: All")
        self.keep_kind_filter_btn.config(text="Type: All")
        self.keep_efs_filter_btn.config(text="EFS: All")
        if self.keep_search_var.get():
            self.keep_search_var.set("")
        else:
            self.refresh_keep_listbox()

    def on_analyze_single(self, event=None):
        sel = self.listbox.curselection()
        if len(sel) != 1:
            messagebox.showinfo(
                "Info",
                "Please select exactly one file for single track analysis"
            )
            return
        path = self.files[sel[0]]
        if (
            self.ultra_precision_var.get()
            and self.analysis_mode_var.get() in (
                ANALYSIS_MODE_INPUT, ANALYSIS_MODE_SPOTIFY
            )
        ):
            self.analyze_files([path], analysis_kind="single_precision")
            return
        self.run_single(path)

    def run_single(self, path):
        if not self.ensure_audio_tools_available():
            return

        target = self.update_targets_from_ui()
        if target is None:
            return
        two_pass = (
            self.two_pass_var.get()
            if analysis_mode(target) == ANALYSIS_MODE_EBU else False
        )
        self.text.delete("1.0", tk.END)
        self.progress_var.set("Analyzing single track...")
        self.last_export_payload = None
        self.current_analysis_snapshot = None
        self.export_btn.config(state=tk.DISABLED)
        self.keep_btn.config(state=tk.DISABLED)
        mode = "2-pass" if two_pass else "1-pass"
        self.text.insert(tk.END, f"Analyzing {analysis_mode_label(target)} ({mode})...\n{path}\n")
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
            raw, parsed, cache_hit = analyze_with_cache(path, two_pass, target)
            parsed = apply_analysis_mode(parsed, target)
        except Exception as exc:
            self.after(0, lambda: self.show_analysis_error(str(exc)))
            return
        self.after(0, lambda: self.show_single(path, parsed, raw, two_pass, metadata, target, cache_hit))
    
    def on_analyze_selected(self):
        indices = self.listbox.curselection()
        if indices:
            selected_files = [self.files[i] for i in indices]
            self.selected_audio_paths = tuple(selected_files)
        else:
            selected_files = [
                path for path in self.selected_audio_paths
                if path in self.files
            ]

        if not selected_files:
            messagebox.showinfo("Info", "Please select tracks")
            return

        self.analyze_files(selected_files, analysis_kind="selection")
    
    def analyze_files(self, files, analysis_kind="selection"):
        files = list(files)
        if not files:
            messagebox.showinfo("Info", "No tracks selected")
            return

        if not self.ensure_audio_tools_available():
            return

        target = self.update_targets_from_ui()
        if target is None:
            return

        self.last_export_payload = None
        self.current_analysis_snapshot = None
        self.analysis_started_at = time.time()
        self.set_controls_enabled(False)
        self.text.delete("1.0", tk.END)
        two_pass = (
            self.two_pass_var.get()
            if analysis_mode(target) == ANALYSIS_MODE_EBU else False
        )
        ultra_precision = (
            analysis_kind in ("album", "selection", "single_precision")
            and analysis_mode(target) in (ANALYSIS_MODE_INPUT, ANALYSIS_MODE_SPOTIFY)
            and bool(self.ultra_precision_var.get())
        )
        mode = "2-pass" if two_pass else "1-pass"
        if analysis_kind == "album":
            label = "album"
        elif analysis_kind == "single_precision":
            label = "single track"
        else:
            label = "selected tracks"
        self.progress_var.set(f"Progress: 0/{len(files)}")
        self.text.insert(
            tk.END,
            f"Analyzing {label}: {analysis_mode_label(target)} ({mode})...\n"
            + (
                f"Ultra-Precision: {ULTRA_CONTEXT_SECONDS:.0f}s context / "
                f"{ULTRA_STEP_SECONDS:.0f}s step (this is substantially slower).\n\n"
                if ultra_precision else "\n"
            ),
        )
        threading.Thread(
            target=self.worker_album,
            args=(files, two_pass, target, analysis_kind, ultra_precision),
            daemon=True
        ).start()
    
    def show_single(
        self, path, parsed, raw, two_pass, metadata, target, cache_hit=False,
        remember_snapshot=True,
    ):
        self.text.delete("1.0", tk.END)

        mode = "2-pass" if two_pass else "1-pass"
        selected_mode = analysis_mode(target)
        self.text.insert(
            tk.END,
            f"File:\n{path}\n\nVerdict / Preview Mode: {analysis_mode_label(target)}\n"
            f"Measurement Pass: {mode}\n\n",
        )

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

        if remember_snapshot:
            self.current_analysis_snapshot = {
                "type": "single",
                "path": path,
                "parsed": dict(parsed),
                "raw": raw,
                "two_pass": two_pass,
                "metadata": dict(metadata),
                "target": dict(target),
            }

        input_lufs = parsed.get("Input Integrated")
        if selected_mode == ANALYSIS_MODE_SPOTIFY and input_lufs is not None:
            applied_gain = parsed.get("Applied Gain", 0.0)
            self.text.insert(
                tk.END,
                f"▶ Input Integrated: {input_lufs:.1f} LUFS\n"
                f"  → Spotify Normal gain: {applied_gain:+.1f} dB\n\n"
            )

        self.text.insert(tk.END, "[Input]\n")
        for key in ["Input Integrated", "Input True Peak", "Input LRA", "Input Threshold"]:
            if key in parsed:
                self.text.insert(tk.END, f"  {key}: {parsed[key]}\n")

        if selected_mode != ANALYSIS_MODE_INPUT:
            output_heading = (
                "[Spotify Normal Preview — gain only]"
                if selected_mode == ANALYSIS_MODE_SPOTIFY
                else f"[EBU R128 Loudnorm Output — {mode}]"
            )
            self.text.insert(tk.END, f"\n{output_heading}\n")
            for key in ["Output Integrated", "Output True Peak", "Output LRA", "Output Threshold"]:
                if key in parsed:
                    self.text.insert(tk.END, f"  {key}: {parsed[key]}\n")

        self.text.insert(tk.END, "\n--- raw loudnorm measurement log ---\n")
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
        self.progress_var.set("Complete (cache hit)" if cache_hit else "Complete")
        self.set_controls_enabled(True)

    def show_analysis_error(self, message):
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, message + "\n")
        self.progress_var.set("Analysis failed")
        self.last_export_payload = None
        self.current_analysis_snapshot = None
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

    def update_ultra_progress(self, done, total, name=None, status=None):
        progress = f"Ultra-Precision windows: {done}/{total}"
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

    def payload_fatigue_dose(self, payload):
        summary = payload.get("summary") or {}
        dose = summary.get("ear_fatigue_dose_efm")
        if dose is not None:
            try:
                return float(dose)
            except (TypeError, ValueError):
                pass
        return equivalent_fatigue_minutes(
            summary.get("listening_fatigue_score"),
            summary.get("total_time_sec"),
        )

    def payload_verdict_method(self, payload):
        target = payload.get("target") or {}
        mode = target.get("analysis_mode")
        if mode == ANALYSIS_MODE_INPUT:
            return "INPUT"
        if mode == ANALYSIS_MODE_SPOTIFY:
            return "SPOTIFY"

        pass_mode = (
            payload.get("mode")
            or (payload.get("metadata") or {}).get("mode")
            or ""
        ).lower()
        if "2-pass" in pass_mode:
            return "EBU-2P"
        if "1-pass" in pass_mode:
            return "EBU-1P"
        if mode == ANALYSIS_MODE_EBU or mode is None:
            return "EBU"
        return "UNKNOWN"

    def on_keep_result(self, automatic=False):
        if self.last_export_payload is None:
            if not automatic:
                messagebox.showinfo("Info", "No results to keep yet.")
            return "unavailable"

        duplicate_key = keep_payload_fingerprint(self.last_export_payload)
        analysis_profile = keep_payload_analysis_profile(self.last_export_payload)
        duplicate = next(
            (
                entry for entry in self.kept_results
                if kept_entry_matches_payload(entry, self.last_export_payload)
            ),
            None,
        )
        if duplicate is not None:
            if automatic:
                return "duplicate"
            self.clear_keep_filters()
            self.refresh_keep_listbox(select_path=duplicate.get("path"))
            label = self.keep_list_label(duplicate)
            self.progress_var.set(f"Already kept: {label}")
            messagebox.showinfo(
                "Duplicate Kept Result",
                "An identical measurement result is already kept.\n\n"
                f"{label}\n\n"
                "The existing result has been selected; no duplicate was added.",
            )
            return "duplicate"

        kept_at = datetime.now()
        artist, title = self.payload_display_parts(self.last_export_payload)
        kind = self.payload_keep_kind(self.last_export_payload)
        score = self.payload_fatigue_score(self.last_export_payload)
        dose = self.payload_fatigue_dose(self.last_export_payload)
        verdict_method = self.payload_verdict_method(self.last_export_payload)
        report_text = self.text.get("1.0", tk.END).rstrip() + "\n"

        record = {
            "kept_at": kept_at.isoformat(timespec="seconds"),
            "kept_at_display": format_keep_datetime(kept_at),
            "kind": kind,
            "artist": artist,
            "title": title,
            "listening_fatigue_score": score,
            "ear_fatigue_dose_efm": dose,
            "verdict_method": verdict_method,
            "analysis_profile": analysis_profile,
            "duplicate_key": duplicate_key,
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
            return "error"

        self.load_kept_results(select_path=path)
        if not automatic:
            self.progress_var.set(f"Kept: {record['kept_at_display']} | {artist} / {title}")
        return "kept"

    def load_kept_results(self, select_path=None):
        indexed = {
            os.path.normcase(os.path.abspath(entry.get("path", ""))): entry
            for entry in load_keep_index()
            if entry.get("path") and os.path.isfile(entry["path"])
        }
        entries = []
        disk_paths = []
        if os.path.isdir(KEEP_DIR):
            disk_paths = [
                os.path.join(KEEP_DIR, name)
                for name in os.listdir(KEEP_DIR)
                if name.lower().endswith(".json")
            ]

        for path in disk_paths:
            key = os.path.normcase(os.path.abspath(path))
            entry = indexed.get(key)
            if (
                entry is None
                or not entry.get("verdict_method")
                or not entry.get("duplicate_key")
                or not entry.get("analysis_profile")
                or "dose_efm" not in entry
            ):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    entry = self.normalize_kept_record(path, data)
                except Exception:
                    entry = None
            if entry is not None:
                entries.append(entry)

        self.kept_results = entries
        save_keep_index(entries)
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

        dose = data.get("ear_fatigue_dose_efm")
        if dose is None:
            dose = self.payload_fatigue_dose(payload)
        try:
            dose = float(dose) if dose is not None else None
        except (TypeError, ValueError):
            dose = None

        return {
            "path": path,
            "kept_at": kept_at,
            "kept_at_display": data.get("kept_at_display") or format_keep_datetime(kept_at),
            "kind": kind,
            "artist": data.get("artist") or artist,
            "title": data.get("title") or title,
            "track_count": (payload.get("summary") or {}).get("track_count") or len(payload.get("tracks") or []),
            "score": score,
            "dose_efm": dose,
            "analysis_profile": (
                data.get("analysis_profile")
                or keep_payload_analysis_profile(payload)
            ),
            "duplicate_key": (
                data.get("duplicate_key")
                or keep_payload_fingerprint(payload)
            ),
            "verdict_method": (
                data.get("verdict_method")
                or self.payload_verdict_method(payload)
            ),
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
            if sort_by == "Ear Fatigue Dose":
                dose = entry.get("dose_efm")
                if dose is None:
                    return float("-inf") if reverse else float("inf")
                return dose
            return str(entry.get("kept_at") or "")

        self.kept_results.sort(key=sort_key, reverse=reverse)
        self.visible_kept_results = [
            entry for entry in self.kept_results
            if self.kept_entry_matches_filters(entry)
        ]
        self.keep_listbox.delete(0, tk.END)
        selected_index = None

        for idx, entry in enumerate(self.visible_kept_results):
            self.keep_listbox.insert(tk.END, self.keep_list_label(entry))
            if select_path and entry.get("path") == select_path:
                selected_index = idx

        self.keep_filter_status_var.set(
            f"Showing {len(self.visible_kept_results)} / {len(self.kept_results)}"
        )

        if selected_index is not None:
            self.keep_listbox.selection_set(selected_index)
            self.keep_listbox.activate(selected_index)
            self.keep_listbox.see(selected_index)

    def kept_entry_matches_filters(self, entry):
        method_filter = self.keep_method_filter_var.get()
        method = str(entry.get("verdict_method") or "UNKNOWN").upper()
        if method_filter == "EBU":
            if not method.startswith("EBU"):
                return False
        elif method_filter != "ALL" and method != method_filter:
            return False

        kind_filter = self.keep_kind_filter_var.get()
        kind = str(entry.get("kind") or "ALBUM").upper()
        if kind_filter != "ALL" and kind != kind_filter:
            return False

        efs_filter = self.keep_efs_filter_var.get()
        if efs_filter != "ALL":
            score = entry.get("score")
            if score is None:
                return False
            try:
                score = float(score)
            except (TypeError, ValueError):
                return False
            if efs_filter == "COMFORTABLE" and not score < 25.0:
                return False
            if efs_filter == "OK" and not 25.0 <= score < 45.0:
                return False
            if efs_filter == "FATIGUING" and not 45.0 <= score < 65.0:
                return False
            if efs_filter == "HEAVY" and not score >= 65.0:
                return False

        query = self.keep_search_var.get().strip().casefold()
        if query:
            searchable = " ".join(
                str(entry.get(field) or "")
                for field in (
                    "artist", "title", "kept_at_display", "kind", "verdict_method"
                )
            ).casefold()
            if query not in searchable:
                return False

        return True

    def keep_list_label(self, entry):
        score = entry.get("score")
        score_text = f"{score:5.1f}" if score is not None else "  -- "
        dose = entry.get("dose_efm")
        dose_text = f"{dose:6.1f}" if dose is not None else "   -- "
        kind = str(entry.get("kind") or "ALBUM")[:6]
        count = entry.get("track_count")
        count_text = f"{count:>2}t" if count and kind != "SINGLE" else "   "
        kind_count_text = f"{kind:<6} {count_text}"
        verdict_method = str(entry.get("verdict_method") or "UNKNOWN")[:7]
        method_display = verdict_method
        if entry.get("analysis_profile") == "ULTRA":
            method_display = f"{verdict_method[:5]}-U"
        return (
            f"{entry.get('kept_at_display', 'Unknown Date')} | "
            f"{kind_count_text} | "
            f"{method_display:<7} | "
            f"EFS {score_text} | "
            f"EFD {dose_text} | "
            f"{entry.get('artist', 'Unknown Artist')} / {entry.get('title', 'Unknown')}"
        )

    def on_load_kept_result(self, event=None):
        if self.analysis_running:
            return
        selection = self.keep_listbox.curselection()
        if not selection:
            return

        index = selection[0]
        if index >= len(self.visible_kept_results):
            return
        entry = self.visible_kept_results[index]
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
        # A kept report may not contain the original loudnorm log.  Do not
        # accidentally recalculate it from the previously analyzed item.
        self.current_analysis_snapshot = None
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, report_text.rstrip() + "\n")
        self.progress_var.set(f"Loaded kept result: {self.keep_list_label(entry)}")
        self.set_controls_enabled(True)

    def on_delete_kept_result(self, event=None):
        if self.analysis_running:
            return
        selection = self.keep_listbox.curselection()
        if not selection:
            messagebox.showinfo("Info", "Please select a kept result to delete.")
            return

        index = selection[0]
        if index >= len(self.visible_kept_results):
            return
        entry = self.visible_kept_results[index]
        label = self.keep_list_label(entry)
        ok = messagebox.askyesno(
            "Delete Kept Result",
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
                    report_text = self.text.get("1.0", tk.END).rstrip()
                    raw = self.last_export_payload.get("raw")
                    if isinstance(raw, str) and raw and "--- raw loudnorm measurement log ---" not in report_text:
                        report_text += "\n\n--- raw loudnorm measurement log ---\n" + raw.rstrip()
                    f.write(report_text + "\n")
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
            precision_windows = payload.get("precision_windows") or []
            single = payload.get("result")
            if tracks:
                writer.writerow([])
                writer.writerow(["tracks"])
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
                    "Applied Gain",
                ]
                writer.writerow(track_keys)
                for row in tracks:
                    writer.writerow([row.get(k, "") for k in track_keys])

            if precision_windows:
                writer.writerow([])
                writer.writerow(["precision_windows"])
                window_keys = [
                    "name",
                    "source_name",
                    "duration",
                    "cell_start",
                    "context_start",
                    "context_duration",
                    "Input Integrated",
                    "Input True Peak",
                    "Input LRA",
                    "Input Threshold",
                    "Output Integrated",
                    "Output True Peak",
                    "Output LRA",
                    "Output Threshold",
                    "Applied Gain",
                    "_silence",
                ]
                writer.writerow(window_keys)
                for row in precision_windows:
                    writer.writerow([row.get(k, "") for k in window_keys])

            if not tracks and single:
                writer.writerow([])
                writer.writerow(["result"])
                writer.writerow(["metric", "value"])
                for key, value in single.items():
                    writer.writerow([key, value])

    def worker_album(self, files, two_pass, target, analysis_kind, ultra_precision=False):
        try:
            self._worker_album_impl(
                files, two_pass, target, analysis_kind, ultra_precision
            )
        except Exception as exc:
            message = f"Analysis stopped unexpectedly:\n{exc}"
            self.after(0, self.show_analysis_error, message)

    def _worker_album_impl(
        self, files, two_pass, target, analysis_kind, ultra_precision=False
    ):
        # --- phase 1: probe metadata (parallel) ---
        probe_cache = collect_probes(files)

        artist, album, year = choose_album_metadata(probe_cache)
        
        # --- phase 2: loudnorm (parallel) ---
        skipped = 0
        silent = 0
        results = []
        valid_paths = []
        cached_count = 0
        
        worker_count = min(4, len(files), max(1, (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
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
                cache_hit = False
                if status == "ok":
                    cache_hit = bool(payload.pop("_cache_hit", False))
                    if cache_hit:
                        cached_count += 1
                    results.append(payload)
                    valid_paths.append(path)
                elif status == "silent":
                    silent += 1
                else:
                    skipped += 1
                done += 1
                display_status = "cached" if status == "ok" and cache_hit else status
                self.after(0, self.update_progress, done, total, os.path.basename(path), display_status)
            results.sort(key=lambda r: natural_sort_key(r["name"]))
        if analysis_mode(target) == ANALYSIS_MODE_SPOTIFY and analysis_kind == "album":
            apply_spotify_album_preview(results)

        precision_results = None
        precision_cached_count = 0
        if ultra_precision and results:
            specs = []
            for path in sorted(valid_paths, key=lambda p: natural_sort_key(os.path.basename(p))):
                specs.extend(ultra_window_specs(path, probe_cache[path]["duration"]))
            if not specs:
                raise RuntimeError("Ultra-Precision could not create any analysis windows.")
            precision_results = []
            ultra_workers = min(4, len(specs), max(1, (os.cpu_count() or 2) // 2))
            with ThreadPoolExecutor(max_workers=ultra_workers) as executor:
                futures = {
                    executor.submit(analyze_ultra_window, spec, target): spec
                    for spec in specs
                }
                total_windows = len(futures)
                done_windows = 0
                for future in as_completed(futures):
                    spec = futures[future]
                    try:
                        status, row, cache_hit = future.result()
                    except Exception:
                        status, row, cache_hit = "invalid", None, False
                    if status == "ok" and row is not None:
                        precision_results.append(row)
                        if cache_hit:
                            precision_cached_count += 1
                    done_windows += 1
                    window_name = (
                        f"{os.path.basename(spec['path'])} @ "
                        f"{format_duration(spec['cell_start'])}"
                    )
                    display_status = "cached" if cache_hit else status
                    self.after(
                        0,
                        self.update_ultra_progress,
                        done_windows,
                        total_windows,
                        window_name,
                        display_status,
                    )
            precision_results.sort(
                key=lambda row: (
                    natural_sort_key(row.get("source_name", "")),
                    row.get("cell_start", 0.0),
                )
            )
            expected_duration = sum(spec["cell_duration"] for spec in specs)
            measured_duration = sum(row["duration"] for row in precision_results)
            if abs(expected_duration - measured_duration) > 0.5:
                missing = expected_duration - measured_duration
                raise RuntimeError(
                    "Ultra-Precision window analysis was incomplete "
                    f"({missing:.1f} seconds missing)."
                )
            if analysis_mode(target) == ANALYSIS_MODE_SPOTIFY and results:
                precision_results = apply_precision_preview_gains(
                    precision_results,
                    results,
                    album_shared=analysis_kind == "album",
                )
        single_file = valid_paths[0] if analysis_kind == "single_precision" and valid_paths else None
        single_title = None
        if single_file is not None:
            single_title = (
                probe_cache.get(single_file, {}).get("title")
                or os.path.splitext(os.path.basename(single_file))[0]
            )
        self.after(0, lambda: self.show_album_summary(
            results, artist, album, year, skipped, silent, two_pass, target,
            analysis_kind, cached_count,
            precision_results=precision_results,
            ultra_precision=ultra_precision,
            precision_cached_count=precision_cached_count,
            single_file=single_file,
            single_title=single_title,
        ))

    def show_album_summary(
        self, results, artist, album, year, skipped, silent, two_pass, target,
        analysis_kind, cached_count=0, remember_snapshot=True,
        precision_results=None, ultra_precision=False,
        precision_cached_count=0,
        single_file=None, single_title=None,
    ):

        total_time = sum(r["duration"] for r in results if r["duration"] > 0)
        if analysis_kind == "album":
            payload_type = "album"
        elif analysis_kind == "single_precision":
            payload_type = "single"
        else:
            payload_type = "selection"

        if remember_snapshot and total_time > 0:
            self.current_analysis_snapshot = {
                "type": (
                    "windowed_single" if analysis_kind == "single_precision"
                    else payload_type
                ),
                "results": [dict(row) for row in results],
                "artist": artist,
                "album": album,
                "year": year,
                "skipped": skipped,
                "silent": silent,
                "two_pass": two_pass,
                "target": dict(target),
                "analysis_kind": analysis_kind,
                "cached_count": cached_count,
                "precision_results": (
                    [dict(row) for row in precision_results]
                    if precision_results is not None else None
                ),
                "ultra_precision": bool(ultra_precision),
                "precision_cached_count": precision_cached_count,
                "single_file": single_file,
                "single_title": single_title,
            }

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
            if single_file:
                self.last_export_payload["file"] = single_file
                self.last_export_payload["metadata"]["title"] = single_title
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

        fatigue_results = (
            precision_results
            if ultra_precision and precision_results
            else results
        )
        fatigue_total_time = sum(
            r["duration"] for r in fatigue_results if r["duration"] > 0
        )
        fatigue_metrics = compute_album_fatigue_metrics(
            fatigue_results, fatigue_total_time, target
        )
        album_fatigue = fatigue_metrics["album_fatigue"]
        fatigue_dose_efm = equivalent_fatigue_minutes(album_fatigue, total_time)
        verdict = fatigue_metrics["verdict"]
        verdict_label = fatigue_metrics["verdict_label"]
        core_album_fatigue = fatigue_metrics["core_album_fatigue"]
        high_risk_time = fatigue_metrics["high_risk_time"]
        high_risk_ratio = fatigue_metrics["high_risk_ratio"]
        high_risk_score = fatigue_metrics["high_risk_score"]
        peak_risk = fatigue_metrics["peak_risk"]
        peak_risk_track = fatigue_metrics["peak_risk_track"]
        clipped_tracks = fatigue_metrics["clipped_tracks"]
        mean_tp_track_risk = fatigue_metrics["mean_tp_track_risk"]
        peak_tp_track_risk = fatigue_metrics["peak_tp_track_risk"]
        peak_tp_value = fatigue_metrics["peak_tp_value"]
        peak_tp_risk_track = fatigue_metrics["peak_tp_risk_track"]
        tp_risk_score = fatigue_metrics["tp_risk_score"]
        source_tp_damage_score = fatigue_metrics["source_tp_damage_score"]
        mean_source_tp_damage = fatigue_metrics["mean_source_tp_damage"]
        peak_source_tp_damage = fatigue_metrics["peak_source_tp_damage"]
        peak_source_tp_value = fatigue_metrics["peak_source_tp_value"]
        peak_source_tp_track = fatigue_metrics["peak_source_tp_track"]
        source_tp_over_tracks = fatigue_metrics["source_tp_over_tracks"]
        flat_time = fatigue_metrics["flat_time"]
        flat_ratio = fatigue_metrics["flat_ratio"]
        flat_score = fatigue_metrics["flat_score"]
        album_normalization_penalty = fatigue_metrics["album_normalization_penalty"]
        peak_normalization_penalty = fatigue_metrics["peak_normalization_penalty"]
        peak_normalization_track = fatigue_metrics["peak_normalization_track"]
        normalization_scored = fatigue_metrics["normalization_scored"]
        album_lufs_exposure_penalty = fatigue_metrics["album_lufs_exposure_penalty"]
        peak_lufs_exposure_penalty = fatigue_metrics["peak_lufs_exposure_penalty"]
        peak_lufs_exposure_track = fatigue_metrics["peak_lufs_exposure_track"]
        mode_risk_score = fatigue_metrics["mode_risk_score"]
        mode_risk_kind = fatigue_metrics["mode_risk_kind"]
        album_plr_risk = fatigue_metrics["album_plr_risk"]
        peak_plr_risk = fatigue_metrics["peak_plr_risk"]
        peak_plr_track = fatigue_metrics["peak_plr_track"]
        album_translation_risk = fatigue_metrics["album_translation_risk"]
        peak_translation_risk = fatigue_metrics["peak_translation_risk"]
        peak_translation_track = fatigue_metrics["peak_translation_track"]
        spotify_persistent_risk = fatigue_metrics["spotify_persistent_risk"]
        efs_weights = fatigue_metrics["efs_weights"]
        metric_prefix = fatigue_metrics["metric_prefix"]
        mode = "2-pass" if two_pass else "1-pass"
        self.last_export_payload = {
            "type": payload_type,
            "metadata": {
                "artist": artist,
                "album": album,
                "year": year,
                "mode": mode,
                "analysis_kind": payload_type,
                "verdict_basis": analysis_mode_label(target),
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
                "efs_version": EFS_VERSION,
                "efs_weights": efs_weights,
                "listening_fatigue_score": album_fatigue,
                "efd_version": EFD_VERSION,
                "ear_fatigue_dose_efm": fatigue_dose_efm,
                "fatigue_analysis": (
                    "ultra_precision_windows" if ultra_precision else "track_units"
                ),
                "ultra_context_seconds": (
                    ULTRA_CONTEXT_SECONDS if ultra_precision else None
                ),
                "ultra_step_seconds": (
                    ULTRA_STEP_SECONDS if ultra_precision else None
                ),
                "ultra_window_count": (
                    len(fatigue_results) if ultra_precision else None
                ),
                "core_album_fatigue": core_album_fatigue,
                "high_risk_time_sec": high_risk_time,
                "high_risk_ratio": high_risk_ratio,
                "high_risk_score": high_risk_score,
                "flat_time_sec": flat_time,
                "flat_ratio": flat_ratio,
                "flat_score": flat_score,
                "tp_metric": metric_prefix,
                "tp_over_tracks": len(clipped_tracks),
                "tp_risk_score": tp_risk_score,
                "mean_tp_track_risk": mean_tp_track_risk,
                "peak_tp_track_risk": peak_tp_track_risk,
                "peak_tp_value": peak_tp_value,
                "peak_tp_risk_track": peak_tp_risk_track,
                "source_tp_damage_score": source_tp_damage_score,
                "source_tp_over_tracks": len(source_tp_over_tracks),
                "mean_source_tp_damage": mean_source_tp_damage,
                "peak_source_tp_damage": peak_source_tp_damage,
                "peak_source_tp_value": peak_source_tp_value,
                "peak_source_tp_track": peak_source_tp_track,
                "normalization_penalty": album_normalization_penalty,
                "normalization_penalty_scored": normalization_scored,
                "lufs_exposure_penalty": album_lufs_exposure_penalty,
                "mode_risk_score": mode_risk_score,
                "mode_risk_kind": mode_risk_kind,
                "plr_risk_score": album_plr_risk,
                "normalization_translation_risk": album_translation_risk,
                "spotify_persistent_density_risk": spotify_persistent_risk,
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
            "precision_windows": fatigue_results if ultra_precision else None,
        }
        if single_file:
            self.last_export_payload["file"] = single_file
            self.last_export_payload["metadata"]["title"] = single_title

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
            "fatigue_results": fatigue_results,
            "ultra_precision": bool(ultra_precision),
            "precision_window_count": (
                len(fatigue_results) if ultra_precision else 0
            ),
            "precision_cached_count": precision_cached_count,
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
            "fatigue_dose_efm": fatigue_dose_efm,
            "verdict": verdict,
            "verdict_label": verdict_label,
            "core_album_fatigue": core_album_fatigue,
            "high_risk_time": high_risk_time,
            "high_risk_ratio": high_risk_ratio,
            "high_risk_score": high_risk_score,
            "peak_risk": peak_risk,
            "peak_risk_track": peak_risk_track,
            "clipped_tracks": clipped_tracks,
            "tp_risk_score": tp_risk_score,
            "mean_tp_track_risk": mean_tp_track_risk,
            "peak_tp_track_risk": peak_tp_track_risk,
            "peak_tp_value": peak_tp_value,
            "peak_tp_risk_track": peak_tp_risk_track,
            "source_tp_damage_score": source_tp_damage_score,
            "source_tp_over_tracks": source_tp_over_tracks,
            "mean_source_tp_damage": mean_source_tp_damage,
            "peak_source_tp_damage": peak_source_tp_damage,
            "peak_source_tp_value": peak_source_tp_value,
            "peak_source_tp_track": peak_source_tp_track,
            "flat_time": flat_time,
            "flat_ratio": flat_ratio,
            "flat_score": flat_score,
            "album_normalization_penalty": album_normalization_penalty,
            "peak_normalization_penalty": peak_normalization_penalty,
            "peak_normalization_track": peak_normalization_track,
            "normalization_scored": normalization_scored,
            "album_lufs_exposure_penalty": album_lufs_exposure_penalty,
            "peak_lufs_exposure_penalty": peak_lufs_exposure_penalty,
            "peak_lufs_exposure_track": peak_lufs_exposure_track,
            "mode_risk_score": mode_risk_score,
            "mode_risk_kind": mode_risk_kind,
            "album_plr_risk": album_plr_risk,
            "peak_plr_risk": peak_plr_risk,
            "peak_plr_track": peak_plr_track,
            "album_translation_risk": album_translation_risk,
            "peak_translation_risk": peak_translation_risk,
            "peak_translation_track": peak_translation_track,
            "spotify_persistent_risk": spotify_persistent_risk,
            "metric_prefix": metric_prefix,
            "efs_version": EFS_VERSION,
        }
        self.text.insert(tk.END, format_album_report_text(report_context))

        # The kept-results Listbox is disabled while analysis is running.
        # Re-enable it before auto-keeping so its delete/insert refresh is not
        # ignored by Tk and the newly saved row appears immediately.
        self.set_controls_enabled(True)

        auto_keep_status = None
        if (
            payload_type == "album"
            and analysis_mode(target) in (ANALYSIS_MODE_INPUT, ANALYSIS_MODE_SPOTIFY)
        ):
            auto_keep_status = self.on_keep_result(automatic=True)

        cache_note = f" | cache hits: {cached_count}/{len(results)}" if cached_count else ""
        status = "Complete" + cache_note
        if ultra_precision:
            status += (
                f" | Ultra windows: {len(fatigue_results)}"
                f" (cache {precision_cached_count}/{len(fatigue_results)})"
            )
        if auto_keep_status == "kept":
            status += " | auto-kept"
        elif auto_keep_status == "duplicate":
            status += " | already kept"
        elif auto_keep_status == "error":
            status += " | auto-keep failed"
        self.progress_var.set(status)
        return auto_keep_status

if __name__ == "__main__":

    app = LufsApp()
    app.mainloop()
