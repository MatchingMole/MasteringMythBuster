[🇯🇵 日本語](README.md) | [🇺🇸 English](README_EN.md)
# 🎚️ LUFS Inspector

> A desktop app for scouting the loudness war—one button at a time.

`LUFS Inspector` is a Python desktop app that analyzes **LUFS, LRA, and True Peak** values in audio files. It estimates how your tracks would behave after streaming normalization and calculates a custom listening fatigue score for an entire album.

Inspect one track under a microscope, analyze a hand-picked selection, or send the whole album in for a checkup.

## ✨ Features

- 🎵 Analyze a **single track, selected tracks, or a complete album**
- 📊 Measure Integrated LUFS, LRA, True Peak, and Loudness Gate Threshold
- 🎯 Estimate normalized output using **1-pass or 2-pass** loudnorm analysis
- 📻 Built-in LUFS target presets:
  - Spotify / YouTube: `-14 LUFS`
  - Apple Music: `-16 LUFS`
  - Broadcast / EBU R128: `-23 LUFS`
  - Custom targets from `-30` to `-5 LUFS`
- 👂 Calculate an album-wide **Listening Fatigue Score**
- 🚨 Flag high-risk listening time, low-LRA sections, peaks above 0 dBTP, and normalization stress
- 📋 Show per-track details plus album-wide averages, minimums, and maximums
- 💾 Keep results inside the app, then reload, sort, or delete them later
- 📤 Export reports as TXT, JSON, or CSV
- ⚡ Analyze multiple tracks with up to four workers in parallel—improving the odds that it finishes before your coffee gets cold

Supported extensions: `.wav` `.flac` `.mp3` `.m4a` `.aac` `.ogg` `.dsf`

> [!NOTE]
> Actual codec support depends on your FFmpeg build.

## 🛠️ Requirements

- Python 3
- Tkinter
- [FFmpeg](https://ffmpeg.org/), including both `ffmpeg` and `ffprobe`

Tkinter is normally included with Python. On Linux, you may need to install it separately through your distribution's package manager—for example, as `python3-tk`.

After installing FFmpeg, make sure both commands are available in your PATH:

```console
ffmpeg -version
ffprobe -version
```

If they both answer, you are ready. If only one answers, the duo is experiencing creative differences.

## 🚀 Running the App

Download or clone this repository, then run the script from its directory:

```console
python LUFS_gui_1.0.py
```

On Windows, if `python` is not recognized, try:

```console
py LUFS_gui_1.0.py
```

The app uses only Python's standard library, so there will be no surprise `pip install` tournament.

## 🎮 How to Use It

1. Click **Select Folder** and choose a folder containing audio files.
2. Choose a loudness target and enable or disable **Use loudnorm 2-pass**.
3. Pick the analysis that fits the occasion.

| Button | What it does |
|---|---|
| Analyze Selected Track (Single) | Performs a detailed analysis of one selected track |
| Analyze Selected Tracks (Multiple) | Analyzes only the tracks currently selected |
| Analyze As an Album | Analyzes every supported file in the folder as one album |

When the analysis is complete, you can also:

- Click **Keep Result** to store the result inside the app
- Click **Export Results** to save it as TXT, JSON, or CSV
- Double-click a kept result to load it again
- Sort kept results by date, artist, or listening fatigue score

## 🧠 1-pass vs. 2-pass

| Mode | Best for |
|---|---|
| 1-pass | Faster analysis when you want a quick overview |
| 2-pass | A more careful estimate that feeds the first measurement into a second pass |

The default is 2-pass. Use 1-pass when time is short and 2-pass when you want to have a serious conversation with the audio. The track will wait. The deadline may not.

## 👂 About the Listening Fatigue Score

Album and multi-track analyses produce a Listening Fatigue Score from 0 to 100.

| Score | Verdict |
|---:|---|
| 0–24.9 | ◎ Comfortable |
| 25–44.9 | O OK |
| 45–64.9 | △ Fatiguing |
| 65–100 | × Heavy Fatigue |

This is a custom heuristic that combines LRA, loudness gate thresholds, prolonged high-risk sections, low-LRA sections, True Peak overs, and the expected cost of normalization.

> [!IMPORTANT]
> The score is a listening reference—not a medical diagnosis, a hearing-safety standard, or an objective rating of audio quality. The final measuring instruments are still your ears and a suitable monitoring environment. Ears are not user-replaceable parts, so please take breaks.

## 📁 Stored Data

Results kept with **Keep Result**, along with window settings, are stored here:

- Windows: `%APPDATA%\LUFS Inspector\`
- Other platforms: `~/LUFS Inspector/`

Kept analyses are stored as JSON files inside the `kept_results` folder.

## 📐 Measurement Notes

- Measurements use FFmpeg's `loudnorm` filter.
- Average album Integrated LUFS is weighted by track duration.
- Files at or below `-70 LUFS` are treated as silence and skipped.
- Output values are theoretical estimates for normalization to the selected target.
- Actual streaming-platform processing and current platform behavior may produce different results.
- The app analyzes audio files but does not modify the originals.

## 🧰 Technical Notes

- GUI: Tkinter
- Loudness analysis: FFmpeg `loudnorm`
- Metadata and duration: ffprobe
- Multi-track analysis: `ThreadPoolExecutor`, up to four workers
- Export formats: TXT, JSON, and CSV

## 🤝 Contributing

Bug reports, improvement ideas, and pull requests are welcome.

Reports such as “the numbers say peace, but my ears report a battlefield” are perfectly valid feedback—especially when accompanied by reproducible steps.

---

**Measure responsibly. Master loudly only when necessary.** 🎛️
