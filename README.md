# ZONKRIK Camera Recovery Studio

Windows desktop tools for old camcorder DVDs, camera cards, saved media images and supported capture devices.

## Features

- Read-only optical imaging, bounded bad-sector retries, checkpoints and saved-image extraction.
- DVD scene indexing, raw MPEG salvage and uncertainty-aware backup-index handling.
- Camera file/photo/RAW preservation, copy verification and MP4 conversion.
- PhotoRec image recovery, untrunc container repair and DirectShow capture controls.
- Elapsed/remaining estimates, result sounds and detailed local failure logs.

Recovery cannot guarantee overwritten or physically unreadable footage. Inferred backup scene indexes can overlap recordings and remain unverified even when an MP4 decodes cleanly. Compatible playback/capture hardware is required for tapes.

## Run from source

Windows, Python 3.12 with Tkinter, and FFmpeg/FFprobe are required. Place `ffmpeg.exe` and `ffprobe.exe` beside `app.py` (official project: https://ffmpeg.org/download.html).

```powershell
python app.py
```

The app currently defaults to saving recovery data under `D:\Downloads\Handycam Video Recovery` and requires a D: output location. Originals are preserved. Read the code before adapting drive-specific defaults to another machine.

Optional recovery helpers can be downloaded using `python scripts/prepare_recovery_helpers.py`. Helper binaries are not included. Their recorded download provenance is in `tools/download_manifest.json`. See upstream license terms before redistribution.

## Tests and packaging

```powershell
python -m unittest discover -s tests -v
python -m pip install -r requirements-dev.txt
.\build.ps1
```

Tests use local D: scratch directories and synthetic media fixtures; some checks require FFmpeg. Passing software tests does not prove compatibility with every camera or damaged disc. Keep `sounds`, `scripts`, optional `tools`, FFmpeg and FFprobe beside the packaged executable.

## Third-party components

See `THIRD-PARTY-NOTICES.txt` and `FFmpeg-LICENSE.txt`. PhotoRec, untrunc and DVD-VR research are credited to their upstream projects. This source publication includes no personal footage, disc images, local settings, runtime packages or recovery logs.
