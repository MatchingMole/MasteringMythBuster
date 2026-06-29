[🇯🇵 日本語](README.md) | [🇺🇸 English](README_EN.md)
# 🎚️ LUFS Inspector

> 音圧戦争の戦況を、ボタンひとつで偵察するデスクトップアプリ。

`LUFS Inspector` は、音声ファイルの **LUFS / LRA / True Peak** を解析し、ストリーミング向けにノーマライズされた場合の理論値や、アルバム全体の「耳疲労スコア」を表示する Python 製GUIツールです。

単曲をじっくり見るもよし。選抜メンバーだけ測るもよし。アルバム丸ごと健康診断に連れていくもよし。

## ✨ 主な機能

- 🎵 **単曲・選択した複数曲・アルバム全体**の3通りで解析
- 📊 Integrated LUFS / LRA / True Peak / Loudness Gate Threshold を表示
- 🎯 ノーマライズ後の理論値を **1-pass / 2-pass** で推定
- 📻 LUFSターゲットのプリセット
  - Spotify / YouTube: `-14 LUFS`
  - Apple Music: `-16 LUFS`
  - Broadcast / EBU R128: `-23 LUFS`
  - `-30`〜`-5 LUFS` のカスタム値
- 👂 アルバム全体の **Listening Fatigue Score（耳疲労スコア）** を算出
- 🚨 高リスク区間、低LRA区間、0 dBTP超過、ノーマライズ負荷をチェック
- 📋 曲ごとの詳細と、アルバム全体の平均・最小・最大値をレポート
- 💾 結果をアプリ内に保存し、あとから呼び出し・並べ替え・削除
- 📤 TXT / JSON / CSV でエクスポート
- ⚡ 複数曲を最大4並列で解析。コーヒーが冷める前に終わる可能性が上昇

対応形式：`.wav` `.flac` `.mp3` `.m4a` `.aac` `.ogg` `.dsf`

> [!NOTE]
> 実際に読み込めるコーデックは、お使いのFFmpegビルドに依存します。

## 🛠️ 必要なもの

- Python 3
- Tkinter
- [FFmpeg](https://ffmpeg.org/)（`ffmpeg` と `ffprobe` の両方）

Python本体には通常Tkinterが同梱されています。Linuxで見つからない場合は、ディストリビューションのパッケージ管理機能から `python3-tk` などを追加してください。

FFmpegをインストールしたら、次のコマンドがどこからでも実行できるようにPATHを設定します。

```console
ffmpeg -version
ffprobe -version
```

2人とも返事をすれば準備完了です。片方しか返事をしない場合、コンビとして少し気まずい状態です。

## 🚀 起動方法

このリポジトリをダウンロードまたはクローンし、スクリプトのある場所で実行します。

```console
python LUFS_gui_1.0.py
```

Windowsで `python` が見つからない場合は、こちらでも起動できます。

```console
py LUFS_gui_1.0.py
```

外部Pythonパッケージは使っていないため、`pip install` 大会は開催されません。

## 🎮 使い方

1. **Select Folder** で音声ファイルの入ったフォルダーを選びます。
2. 解析ターゲットと、必要に応じて **Use loudnorm 2-pass** を選びます。
3. 目的に合わせて解析ボタンを押します。

| ボタン | すること |
|---|---|
| Analyze Selected Track (Single) | 選択した1曲を詳しく解析 |
| Analyze Selected Tracks (Multiple) | 選択した曲だけをまとめて解析 |
| Analyze As an Album | フォルダー内の対応ファイルをアルバムとして解析 |

解析が終わったら、次の操作もできます。

- **Keep Result**：結果をアプリ内に保存
- **Export Results**：TXT / JSON / CSVとして保存
- 保存済み結果をダブルクリック：再表示
- 保存済み結果を日付・アーティスト・耳疲労スコアで並べ替え

## 🧠 1-passと2-passの違い

| モード | 特徴 |
|---|---|
| 1-pass | 速い。まず全体の傾向を見たいとき向け |
| 2-pass | 1回目の測定値を2回目へ渡すため、より丁寧な推定向け |

初期設定は2-passです。急いでいる日は1-pass、音と向き合う日は2-pass。音源は逃げませんが締切は逃げます。

## 👂 耳疲労スコアについて

アルバム／複数曲解析では、0〜100の Listening Fatigue Score と判定を表示します。

| スコア | 判定 |
|---:|---|
| 0〜24.9 | ◎ Comfortable |
| 25〜44.9 | O OK |
| 45〜64.9 | △ Fatiguing |
| 65〜100 | × Heavy Fatigue |

スコアは、LRAとラウドネスゲート、長時間続く高リスク区間、低LRA区間、True Peak超過、ノーマライズ時の負荷などを組み合わせた、このアプリ独自のヒューリスティックです。

> [!IMPORTANT]
> このスコアは聴感の参考情報であり、医学的な診断、聴覚安全基準、音質の優劣を示すものではありません。最後に信じるべき測定器は、あなたの耳と適切なモニター環境です。耳は交換部品ではないので休憩もどうぞ。

## 📁 保存場所

**Keep Result** で保持した結果と画面設定は、次の場所に保存されます。

- Windows: `%APPDATA%\LUFS Inspector\`
- その他の環境: `~/LUFS Inspector/`

保持した解析結果は `kept_results` フォルダー内のJSONファイルです。

## 📐 測定について

- 測定にはFFmpegの `loudnorm` フィルターを使用します。
- アルバムの平均Integrated LUFSは、曲の長さを考慮した時間加重平均です。
- `-70 LUFS` 以下のファイルは無音としてスキップします。
- 出力値は指定ターゲットへノーマライズした場合の理論値です。
- ストリーミングサービス側の実際の処理や仕様により、結果が異なる場合があります。
- このアプリは音声ファイルを解析しますが、元ファイル自体を書き換えません。

## 🧰 技術メモ

- GUI: Tkinter
- Loudness analysis: FFmpeg `loudnorm`
- Metadata / duration: ffprobe
- Multi-track analysis: `ThreadPoolExecutor`（最大4ワーカー）
- Export: TXT / JSON / CSV

## 🤝 コントリビュート

バグ報告、改善案、プルリクエスト歓迎です。

「この音源、数字では平和なのに耳が戦場なんですが？」のような報告も、再現条件があれば立派なフィードバックです。

---

**Measure responsibly. Master loudly only when necessary.** 🎛️
