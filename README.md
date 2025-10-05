# FFMPEG GUI

A Python-based graphical user interface for ffmpeg, designed to simplify video and audio processing tasks.

## Features

- **Video Conversion:** Convert video files to various formats like MP4, MKV, MOV, and GIF.
- **Image Sequence to Video:** Create a video from a sequence of images (e.g., `image-001.png`, `image-002.png`, ...).
- **Folder to Video:** Create a video from a folder containing images.
- **Frame Extraction:** Extract a single frame from a video by specifying a timestamp or a frame number.
- **Audio Extraction:** Extract the audio track from a video file and save it as MP3, WAV, or AAC.
- **Merge Video and Audio:** Combine a video file with a separate audio file.
- **Add Subtitles:** Embed subtitles (SRT, ASS) into a video file.
- **Change Video Speed:** Speed up or slow down a video.
- **Hardware Acceleration:** Automatically uses NVIDIA (NVENC), Intel (QSV), or AMD (AMF) encoders for faster processing when available.
- **Job Queue:** Add multiple tasks to a queue and run them sequentially.
- **Drag and Drop:** Easily add files by dragging and dropping them into the application.

## Installation

1.  Make sure Python is installed on your system.
2.  Install the required dependencies:
    ```bash
    pip install -r requirements.txt
    ```

## Usage

Run the application using the start script:
```bash
# On Windows
.\start_gui.bat
```