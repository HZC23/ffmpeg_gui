import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import threading
import os
import tempfile
import queue
import re
import time
import sys
import os
from pathlib import Path
import queue
import shutil
import logging
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Callable, Tuple
import functools

LOG_FILE = Path.home() / ".ffmpeg_gui" / "app.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def get_application_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(__file__)

def get_executable_path(name):
    application_path = get_application_path()
    executable_path = os.path.join(application_path, name)
    if os.path.exists(executable_path):
        return executable_path
    
    return shutil.which(name)

FFMPEG_PATH = get_executable_path("ffmpeg.exe")
FFPROBE_PATH = get_executable_path("ffprobe.exe")

# =============================================================================
# DÉTECTION D'ENCODEUR ET OPTIMISATION
# =============================================================================
@functools.lru_cache(maxsize=1)
def get_available_encoders():
    """Exécute ffmpeg -encoders et met en cache la sortie pour analyse."""
    logger.info("Détection des encodeurs FFmpeg disponibles...")
    if not FFMPEG_PATH:
        logger.error("La commande ffmpeg est introuvable.")
        return ""
    try:
        # Utilise une commande qui se termine pour éviter de bloquer
        result = subprocess.run(
            [FFMPEG_PATH, "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False # Ne pas lever d'exception si ffmpeg retourne un code d'erreur
        )
        if result.returncode == 0:
            logger.info("Encodeurs détectés avec succès.")
            return result.stdout
        else:
            logger.error(f"ffmpeg -encoders a retourné le code d'erreur {result.returncode}: {result.stderr}")
            return ""
    except FileNotFoundError:
        logger.error("La commande ffmpeg est introuvable lors de la détection des encodeurs.")
        return ""
    except Exception as e:
        logger.exception(f"Une erreur inattendue est survenue lors de la détection des encodeurs: {e}")
        return ""

def select_best_video_codec(quality_crf=23, for_hevc=False):
    """
    Sélectionne le meilleur codec vidéo disponible et retourne le nom du codec et les options associées.
    Priorité : NVIDIA NVENC > Intel QSV > AMD AMF > CPU (libx264/libx265).
    """
    encoders = get_available_encoders()
    
    # Mappage de la qualité CRF vers des valeurs spécifiques au codec
    # C'est une approximation, mais elle sert de point de départ raisonnable.
    quality_map = {
        "nvenc": quality_crf,
        "qsv": quality_crf,
        "amf": quality_crf + 5, # AMF a tendance à nécessiter un QP plus élevé pour une qualité similaire
    }

    # Codecs HEVC (H.265)
    if for_hevc:
        if "hevc_nvenc" in encoders:
            logger.info("Encodeur sélectionné : hevc_nvenc (NVIDIA)")
            return "hevc_nvenc", {"-preset": "p6", "-rc": "vbr", "-cq": str(quality_map['nvenc']), "-b:v": "0"}
        if "hevc_qsv" in encoders:
            logger.info("Encodeur sélectionné : hevc_qsv (Intel)")
            return "hevc_qsv", {"-preset": "veryfast", "-global_quality": str(quality_map['qsv'])}
        if "hevc_amf" in encoders:
            logger.info("Encodeur sélectionné : hevc_amf (AMD)")
            return "hevc_amf", {"-rc": "cqp", "-qp_i": str(quality_map['amf']), "-qp_p": str(quality_map['amf']), "-quality": "quality"}
        logger.info("Encodeur HEVC matériel non trouvé, fallback sur libx265 (CPU).")
        return "libx265", {"-preset": "veryfast", "-crf": str(quality_crf)}

    # Codecs H.264
    if "h264_nvenc" in encoders:
        logger.info("Encodeur sélectionné : h264_nvenc (NVIDIA)")
        return "h264_nvenc", {"-preset": "p6", "-rc": "vbr", "-cq": str(quality_map['nvenc']), "-b:v": "0"}
    if "h264_qsv" in encoders:
        logger.info("Encodeur sélectionné : h264_qsv (Intel)")
        return "h264_qsv", {"-preset": "veryfast", "-global_quality": str(quality_map['qsv'])}
    if "h264_amf" in encoders:
        logger.info("Encodeur sélectionné : h264_amf (AMD)")
        return "h264_amf", {"-rc": "cqp", "-qp_i": str(quality_map['amf']), "-qp_p": str(quality_map['amf']), "-quality": "quality"}
    
    logger.info("Encodeur H.264 matériel non trouvé, fallback sur libx264 (CPU).")
    return "libx264", {"-preset": "veryfast", "-crf": str(quality_crf), "-threads": "0"}


logger = logging.getLogger("ffmpeg_gui")
logger.setLevel(logging.DEBUG)
handler = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# --- Robust TkDND init (placer après les imports existants) ---
DND_FILES = None
TkinterDnD = None
try:
    import tkinterdnd2 as _tkdnd_pkg
    tkdnd_base = Path(_tkdnd_pkg.__file__).parent / "tkdnd"
    tkdnd_path = None
    if tkdnd_base.is_dir():
        for name in tkdnd_base.iterdir():
            lower = name.name.lower()
            if sys.platform.startswith("win") and "win" in lower:
                tkdnd_path = str(name); break
            if sys.platform.startswith("linux") and "linux" in lower:
                tkdnd_path = str(name); break
            if sys.platform == "darwin" and ("osx" in lower or "mac" in lower):
                tkdnd_path = str(name); break
        if not tkdnd_path:
            tkdnd_path = str(tkdnd_base)
        os.environ["TKDND_LIBRARY"] = tkdnd_path
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    class TkinterDnD:
        class Tk(tk.Tk): pass

# Shared queue pour logs
_ffmpeg_log_queue = queue.Queue()

def _ffmpeg_worker(command: List[str], on_finish: Optional[Callable[[bool], None]] = None):
    """Thread worker to run ffmpeg and push stdout lines to queue."""
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "universal_newlines": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except FileNotFoundError:
        _ffmpeg_log_queue.put(("error", "ffmpeg not found"))
        if on_finish: on_finish(False)
        return

    # store process so we can terminate it
    _ffmpeg_log_queue.put(("proc", proc))
    for line in proc.stdout:
        _ffmpeg_log_queue.put(("line", line.rstrip("\n")))
    proc.wait()
    code = proc.returncode
    _ffmpeg_log_queue.put(("done", code))
    if on_finish: on_finish(code == 0)

def start_ffmpeg_thread(command: List[str], on_finish: Optional[Callable[[bool], None]] = None) -> threading.Thread:
    t = threading.Thread(target=_ffmpeg_worker, args=(command, on_finish), daemon=True)
    t.start()
    return t

def _generate_ffmpeg_command_and_output(mode: str, settings: dict, inputs: List[str], find_image_sequence_pattern_func: Callable[[List[str]], Optional[dict]]) -> Tuple[List[str], str]:
    command, output_file = [], ""
    input_file = inputs[0]
    base_name = Path(input_file).stem
    try:
        if mode == "convert":
            output_file = f"{base_name}_converted.{settings['output_format']}"
            command = [FFMPEG_PATH]
            if settings['trim_start']: command.extend(["-ss", settings['trim_start']])
            command.extend(["-i", input_file])
            if settings['trim_end']: command.extend(["-to", settings['trim_end']])
            
            # --- Logique d'encodage optimisée ---
            is_hevc = settings['output_format'] in ['mkv', 'mp4'] # Potentiellement utiliser HEVC pour mkv/mp4
            codec, codec_opts = select_best_video_codec(settings['crf'], for_hevc=is_hevc)
            command.extend(["-c:v", codec])
            for opt, val in codec_opts.items():
                command.extend([opt, val])
            # --- Fin de la logique ---

            command.extend(["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", output_file, "-y"])

        elif mode == "img_seq":
            seq_info = find_image_sequence_pattern_func(inputs)
            if not seq_info: raise ValueError("Impossible de détecter une séquence d'images.")
            output_file = str(Path(input_file).parent / f"{seq_info['prefix']}_video.{settings['output_format']}")
            
            # --- Logique d'encodage optimisée ---
            pix_fmt = "yuv420p"
            if settings['output_format'] == 'webm':
                codec = 'libvpx-vp9'
                pix_fmt = 'yuva420p' # Pour la transparence
                codec_opts = {"-crf": str(settings['crf']), "-b:v": "0"}
            else:
                is_hevc = settings['output_format'] in ['mkv', 'mp4']
                codec, codec_opts = select_best_video_codec(settings['crf'], for_hevc=is_hevc)

            command = [FFMPEG_PATH, "-framerate", settings['framerate'], "-start_number", str(seq_info['start']), "-i", seq_info['pattern'], "-c:v", codec]
            for opt, val in codec_opts.items():
                command.extend([opt, val])
            command.extend(["-pix_fmt", pix_fmt, output_file, "-y"])
            # --- Fin de la logique ---

        elif mode == "vid_seq":
            output_file = str(Path(input_file).parent / settings['output_pattern'])
            command = [FFMPEG_PATH, "-i", input_file, output_file, "-y"]
        elif mode == "extract_image":
            output_ext = settings['format']
            if settings["extract_mode"] == "Timestamp":
                if not settings['timestamp']: raise ValueError("Timestamp non spécifié.")
                time_str = settings['timestamp'].replace(':', '-')
                output_file = f"{base_name}_frame_at_{time_str}.{output_ext}"
                command = [FFMPEG_PATH, "-ss", settings['timestamp'], "-i", input_file, "-vframes", "1", output_file, "-y"]
            else: # Frame Number
                if not settings['frame_number']: raise ValueError("Numéro de frame non spécifié.")
                frame_num = int(settings['frame_number']) - 1
                if frame_num < 0: raise ValueError("Le numéro de frame doit être positif.")
                output_file = f"{base_name}_frame_{frame_num + 1}.{output_ext}"
                command = [FFMPEG_PATH, "-i", input_file, "-vf", f"select=eq(n\,{frame_num})", "-vframes", "1", output_file, "-y"]
        elif mode == "extract_audio":
            ext = settings['audio_format']
            codec = {"mp3": "libmp3lame", "wav": "pcm_s16le", "aac": "aac"}[ext]
            output_file = f"{base_name}_audio.{ext}"
            command = [FFMPEG_PATH, "-i", input_file, "-vn", "-acodec", codec, "-q:a", "2", output_file, "-y"]
        elif mode == "merge":
            if not settings['secondary_file']: raise ValueError("Fichier secondaire non spécifié.")
            output_file = f"{base_name}_merged.mp4"
            command = [FFMPEG_PATH, "-i", input_file, "-i", settings['secondary_file'], "-c:v", "copy", "-c:a", "aac", "-shortest", output_file, "-y"]
        elif mode == "subtitles":
            if not settings['secondary_file']: raise ValueError("Fichier de sous-titres non spécifié.")
            output_file = f"{base_name}_subtitled.mkv"
            command = [FFMPEG_PATH, "-i", input_file, "-i", settings['secondary_file'], "-c", "copy", "-c:s", "srt", output_file, "-y"]
        elif mode == "speed":
            speed = float(str(settings['speed_factor']).replace(',', '.'))
            output_file = f"{base_name}_speed_{speed}x.mp4"
            command = [FFMPEG_PATH, "-i", input_file, "-filter_complex", f"[0:v]setpts={1/speed}*PTS[v];[0:a]atempo={speed}[a]", "-map", "[v]", "-map", "[a]", output_file, "-y"]
        return command, output_file
    except Exception as e:
        logger.exception(f"Erreur lors de la préparation de la commande pour le mode {mode}: {e}")
        messagebox.showerror("Erreur de préparation", f"Une erreur est survenue lors de la préparation de la commande FFmpeg pour le mode {mode}. Voir les logs pour plus de détails.")
        return [], ""

# =============================================================================
# THEME ET STYLE
# =============================================================================
class AppTheme:
    COLOR_BACKGROUND = "#1E1E1E"
    COLOR_FRAME = "#252526"
    COLOR_FRAME_BORDER = "#333333"
    COLOR_TEXT = "#E0E0E0"
    COLOR_TEXT_DISABLED = "#707070"
    COLOR_ACCENT = "#007ACC"
    COLOR_ACCENT_HOVER = "#0099E6"
    COLOR_SUCCESS = "#28a745"
    COLOR_ERROR = "#d9534f"

    FONT_FAMILY = "Segoe UI"
    FONT_NORMAL = (FONT_FAMILY, 13)
    FONT_BOLD = (FONT_FAMILY, 13, "bold")
    FONT_LARGE_BOLD = (FONT_FAMILY, 16, "bold")
    FONT_MONO = ("Consolas", 12)

# =============================================================================
# MOTEUR D'ANIMATION
# =============================================================================
class Animator:
    def __init__(self, widget):
        self.widget = widget
        self.animation_id = None

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb_color):
        return f'#{rgb_color[0]:02x}{rgb_color[1]:02x}{rgb_color[2]:02x}'

    def _interpolate_color(self, start_color, end_color, fraction):
        start_rgb = self._hex_to_rgb(start_color)
        end_rgb = self._hex_to_rgb(end_color)
        new_rgb = [
            int(start_rgb[i] + (end_rgb[i] - start_rgb[i]) * fraction)
            for i in range(3)
        ]
        return self._rgb_to_hex(new_rgb)

    def animate(self, prop, end_val, duration):
        if self.animation_id:
            self.widget.after_cancel(self.animation_id)

        start_val = self.widget.cget(prop)
        start_time = time.monotonic()

        def step():
            elapsed_ms = (time.monotonic() - start_time) * 1000
            fraction = min(elapsed_ms / duration, 1.0)

            new_color = self._interpolate_color(start_val, end_val, fraction)
            try:
                self.widget.configure({prop: new_color})
            except tk.TclError:
                pass # Widget might be destroyed

            if fraction < 1.0:
                self.animation_id = self.widget.after(10, step)
            else:
                self.animation_id = None
        step()

# =============================================================================
# WIDGETS PERSONNALISÉS
# =============================================================================
class AnimatedButton(ctk.CTkButton):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._base_fg_color = self.cget("fg_color")
        self._hover_fg_color = self.cget("hover_color")
        self._is_active = False

    def set_active(self, is_active):
        self._is_active = is_active
        if is_active:
            self.configure(fg_color=AppTheme.COLOR_ACCENT)
        else:
            self.configure(fg_color=self._base_fg_color)

class FileListItem(ctk.CTkFrame):
    def __init__(self, master, filepath, on_remove, is_folder=False):
        super().__init__(master, fg_color=AppTheme.COLOR_FRAME_BORDER, corner_radius=5)
        self.filepath = filepath
        self.on_remove = on_remove
        self.grid_columnconfigure(0, weight=1)
        display_name = Path(filepath).name
        if is_folder:
            display_name = f"📁 {display_name}"
        ctk.CTkLabel(self, text=display_name, font=AppTheme.FONT_NORMAL, anchor="w").grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        ctk.CTkButton(self, text="✕", width=28, height=28, fg_color="transparent", hover_color=AppTheme.COLOR_ERROR, command=self.remove_self).grid(row=0, column=1, padx=5, pady=5)

    def remove_self(self):
        self.on_remove(self.filepath, self)

class QueueListItem(ctk.CTkFrame):
    def __init__(self, master, job_data, on_remove):
        super().__init__(master, fg_color=AppTheme.COLOR_FRAME_BORDER, corner_radius=5)
        self.job_data = job_data
        self.on_remove = on_remove
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=job_data['description'], font=AppTheme.FONT_NORMAL, anchor="w").grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        ctk.CTkButton(self, text="✕", width=28, height=28, fg_color="transparent", hover_color=AppTheme.COLOR_ERROR, command=self.remove_self).grid(row=0, column=1, padx=5, pady=5)

    def remove_self(self):
        self.on_remove(self.job_data, self)

# =============================================================================
# FRAMES D'OPTIONS SPÉCIFIQUES
# =============================================================================
class BaseTaskFrame(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=AppTheme.COLOR_FRAME, corner_radius=10, **kwargs)
        self.animator = Animator(self)

    def get_settings(self): return {}

    def appear(self):
        self.tkraise()
        self.configure(fg_color=AppTheme.COLOR_BACKGROUND)
        self.animator.animate("fg_color", AppTheme.COLOR_FRAME, 300)

class ConvertFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(1, weight=1)
        self.output_format = tk.StringVar(value="mp4")
        self.crf_value = tk.IntVar(value=23)
        self.trim_start = tk.StringVar()
        self.trim_end = tk.StringVar()

        ctk.CTkLabel(self, text="Format de sortie:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSegmentedButton(self, values=["mp4", "mkv", "mov", "gif"], variable=self.output_format, selected_color=AppTheme.COLOR_ACCENT, selected_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=0, column=1, padx=10, pady=10, sticky="w")
        ctk.CTkLabel(self, text="Qualité (CRF):", font=AppTheme.FONT_BOLD).grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSlider(self, from_=17, to=30, number_of_steps=13, variable=self.crf_value, button_color=AppTheme.COLOR_ACCENT, button_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=1, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(self, textvariable=self.crf_value).grid(row=1, column=2, padx=10)
        ctk.CTkLabel(self, text="Début (HH:MM:SS):", font=AppTheme.FONT_BOLD).grid(row=2, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.trim_start).grid(row=2, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(self, text="Fin (HH:MM:SS):", font=AppTheme.FONT_BOLD).grid(row=3, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.trim_end).grid(row=3, column=1, padx=10, pady=10, sticky="ew")

    def get_settings(self):
        return {"output_format": self.output_format.get(), "crf": self.crf_value.get(), "trim_start": self.trim_start.get(), "trim_end": self.trim_end.get()}

class ImageSequenceFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.output_format = tk.StringVar(value="mp4")
        self.framerate = tk.StringVar(value="25")
        self.crf_value = tk.IntVar(value=22)

        ctk.CTkLabel(self, text="Format de sortie:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSegmentedButton(self, values=["mp4", "mkv", "mov", "webm"], variable=self.output_format, selected_color=AppTheme.COLOR_ACCENT, selected_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=0, column=1, padx=10, pady=10, sticky="w")
        ctk.CTkLabel(self, text="Images par seconde (FPS):", font=AppTheme.FONT_BOLD).grid(row=1, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.framerate).grid(row=1, column=1, padx=10, pady=10, sticky="w")
        ctk.CTkLabel(self, text="Qualité (CRF):", font=AppTheme.FONT_BOLD).grid(row=2, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSlider(self, from_=17, to=30, number_of_steps=13, variable=self.crf_value, button_color=AppTheme.COLOR_ACCENT, button_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=2, column=1, padx=10, pady=10, sticky="ew")
        ctk.CTkLabel(self, textvariable=self.crf_value).grid(row=2, column=2, padx=10)

    def get_settings(self):
        return {"output_format": self.output_format.get(), "framerate": self.framerate.get(), "crf": self.crf_value.get()}

class VidSeqFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.output_pattern = tk.StringVar(value="image-%04d.png")
        ctk.CTkLabel(self, text="Pattern de sortie:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.output_pattern, width=250).grid(row=0, column=1, padx=10, pady=10, sticky="w")

    def get_settings(self):
        return {"output_pattern": self.output_pattern.get()}

class ExtractAudioFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.audio_format = tk.StringVar(value="mp3")
        ctk.CTkLabel(self, text="Format Audio:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSegmentedButton(self, values=["mp3", "wav", "aac"], variable=self.audio_format, selected_color=AppTheme.COLOR_ACCENT, selected_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=0, column=1, padx=10, pady=10, sticky="w")

    def get_settings(self):
        return {"audio_format": self.audio_format.get()}

class ExtractImageFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.extract_mode = tk.StringVar(value="Timestamp")
        self.timestamp = tk.StringVar(value="00:00:01")
        self.frame_number = tk.StringVar(value="25")
        self.image_format = tk.StringVar(value="png")

        ctk.CTkLabel(self, text="Mode d'extraction:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSegmentedButton(self, values=["Timestamp", "Numéro"], variable=self.extract_mode, command=self.update_ui, selected_color=AppTheme.COLOR_ACCENT, selected_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=0, column=1, padx=10, pady=10, sticky="w")

        self.timestamp_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkLabel(self.timestamp_frame, text="Timestamp (HH:MM:SS):", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.timestamp_frame, textvariable=self.timestamp).grid(row=0, column=1, padx=10, pady=10, sticky="w")

        self.framenum_frame = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkLabel(self.framenum_frame, text="Numéro de l'image:", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self.framenum_frame, textvariable=self.frame_number).grid(row=0, column=1, padx=10, pady=10, sticky="w")

        ctk.CTkLabel(self, text="Format de l'image:", font=AppTheme.FONT_BOLD).grid(row=2, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkSegmentedButton(self, values=["png", "jpg"], variable=self.image_format, selected_color=AppTheme.COLOR_ACCENT, selected_hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=2, column=1, padx=10, pady=10, sticky="w")
        self.update_ui()

    def update_ui(self, _=None):
        if self.extract_mode.get() == "Timestamp":
            self.timestamp_frame.grid(row=1, column=0, columnspan=2, sticky="ew")
            self.framenum_frame.grid_forget()
        else:
            self.framenum_frame.grid(row=1, column=0, columnspan=2, sticky="ew")
            self.timestamp_frame.grid_forget()

    def get_settings(self):
        return {"extract_mode": self.extract_mode.get(), "timestamp": self.timestamp.get(), "frame_number": self.frame_number.get(), "format": self.image_format.get()}

class BrowseFileFrame(BaseTaskFrame):
    def __init__(self, master, label_text="Fichier:", file_types=None, **kwargs):
        super().__init__(master, **kwargs)
        self.file_path = tk.StringVar()
        self.file_types = file_types or [("Tous les fichiers", "*.*")]
        ctk.CTkLabel(self, text=label_text, font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.entry = ctk.CTkEntry(self, textvariable=self.file_path, state="readonly", width=300)
        self.entry.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        ctk.CTkButton(self, text="Parcourir...", command=self.browse, fg_color=AppTheme.COLOR_ACCENT, hover_color=AppTheme.COLOR_ACCENT_HOVER).grid(row=1, column=1, padx=10, pady=10)

    def browse(self):
        path = filedialog.askopenfilename(filetypes=self.file_types)
        if path: self.file_path.set(path)

    def get_settings(self):
        return {"secondary_file": self.file_path.get()}

class SpeedFrame(BaseTaskFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.speed_factor = tk.StringVar(value="1.0")
        ctk.CTkLabel(self, text="Facteur de vitesse (ex: 2.0 ou 0.5):", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkEntry(self, textvariable=self.speed_factor).grid(row=0, column=1, padx=10, pady=10, sticky="w")

    def get_settings(self):
        return {"speed_factor": self.speed_factor.get()}

# =============================================================================
# PANNEAUX PRINCIPAUX
# =============================================================================
class Sidebar(ctk.CTkFrame):
    def __init__(self, master, controller, **kwargs):
        super().__init__(master, **kwargs)
        self.controller = controller
        self.configure(fg_color=AppTheme.COLOR_FRAME, corner_radius=0)
        self.grid_columnconfigure(0, weight=1)
        self.task_buttons = {}

        ctk.CTkLabel(self, text="FFMPEG STUDIO", font=AppTheme.FONT_LARGE_BOLD, text_color=AppTheme.COLOR_ACCENT).grid(row=0, column=0, padx=20, pady=20, sticky="ew")

        tasks = {
            "convert": ("🔄", "Convertir"), "img_seq": ("🎞️", "Images -> Vidéo"),
            "vid_seq": ("🖼️", "Vidéo -> Images"), "extract_image": ("📷", "Extraire Image"),
            "extract_audio": ("🎵", "Extraire l'Audio"), "merge": ("🔗", "Fusionner"),
            "subtitles": ("📄", "Sous-titres"), "speed": ("⏩", "Vitesse"),
            "folders_to_videos": ("📁▶️", "Dossiers -> Vidéos")
        }
        for i, (key, (icon, text)) in enumerate(tasks.items()):
            btn = AnimatedButton(self, text=f"{icon}  {text}", command=lambda k=key: self.select_task(k), font=AppTheme.FONT_BOLD, anchor="w", fg_color="transparent", hover_color=AppTheme.COLOR_FRAME_BORDER, corner_radius=5, height=35)
            btn.grid(row=i + 1, column=0, padx=10, pady=5, sticky="ew")
            self.task_buttons[key] = btn

    def select_task(self, task_key):
        self.controller.set_operation_mode(task_key)
        for key, btn in self.task_buttons.items():
            btn.set_active(key == task_key)

class ContentPanel(ctk.CTkFrame):
    def __init__(self, master, controller, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.controller = controller
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.file_panel = ctk.CTkFrame(self, fg_color=AppTheme.COLOR_FRAME, corner_radius=10)
        self.file_panel.grid(row=0, column=0, sticky="ew", padx=(10, 0), pady=(10, 5))
        self.file_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.file_panel, text="Fichiers d'Entrée (Glissez-déposez ici)", font=AppTheme.FONT_BOLD).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        # Création d'un container tk natif pour DnD (plus fiable que d'enregistrer sur un widget CTk)
        self._dnd_target = tk.Frame(self.file_panel, bg=AppTheme.COLOR_BACKGROUND)
        self._dnd_target.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)

        # Le scrollable CTk est placé DANS ce container (apparence inchangée pour l'utilisateur)
        self.file_scroll_frame = ctk.CTkScrollableFrame(self._dnd_target, height=120, fg_color=AppTheme.COLOR_BACKGROUND, border_color=AppTheme.COLOR_FRAME_BORDER, border_width=1)
        self.file_scroll_frame.pack(fill="both", expand=True)

        # Enregistrement du DnD sur le widget tk natif (si disponible)
        if DND_FILES:
            try:
                self._dnd_target.drop_target_register(DND_FILES)
                self._dnd_target.dnd_bind('<<Drop>>', self.controller.handle_drop)
                self._dnd_target.dnd_bind('<<DragEnter>>', self.on_drag_enter)
                self._dnd_target.dnd_bind('<<DragLeave>>', self.on_drag_leave)
            except Exception:
                # dernier recours : tenter sur la CTkScrollableFrame si elle supporte l'API
                if hasattr(self.file_scroll_frame, "drop_target_register"):
                    self.file_scroll_frame.drop_target_register(DND_FILES)
                    self.file_scroll_frame.dnd_bind('<<Drop>>', self.controller.handle_drop)
                    self.file_scroll_frame.dnd_bind('<<DragEnter>>', self.on_drag_enter)
                    self.file_scroll_frame.dnd_bind('<<DragLeave>>', self.on_drag_leave)

        self.tab_view = ctk.CTkTabview(self, fg_color=AppTheme.COLOR_FRAME, segmented_button_selected_color=AppTheme.COLOR_ACCENT, border_color=AppTheme.COLOR_FRAME_BORDER, border_width=1, corner_radius=10)
        self.tab_view.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(5, 10))
        self.tab_view.add("Options")
        self.tab_view.add("File d'attente")
        self.tab_view.add("Logs")

        self.options_panel = self.tab_view.tab("Options")
        self.options_panel.grid_columnconfigure(0, weight=1); self.options_panel.grid_rowconfigure(0, weight=1)
        self.task_frames = {
            "convert": ConvertFrame(self.options_panel), "img_seq": ImageSequenceFrame(self.options_panel),
            "vid_seq": VidSeqFrame(self.options_panel), "extract_audio": ExtractAudioFrame(self.options_panel),
            "extract_image": ExtractImageFrame(self.options_panel),
            "merge": BrowseFileFrame(self.options_panel, label_text="Fichier audio/vidéo à fusionner:"),
            "subtitles": BrowseFileFrame(self.options_panel, label_text="Fichier de sous-titres (.srt, .ass):", file_types=[("Subtitle Files", "*.srt *.ass *.vtt")]),
            "speed": SpeedFrame(self.options_panel),
            "folders_to_videos": ImageSequenceFrame(self.options_panel)
        }
        for frame in self.task_frames.values():
            frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        self.queue_panel = self.tab_view.tab("File d'attente")
        self.queue_panel.grid_columnconfigure(0, weight=1); self.queue_panel.grid_rowconfigure(0, weight=1)
        self.queue_scroll_frame = ctk.CTkScrollableFrame(self.queue_panel, fg_color=AppTheme.COLOR_BACKGROUND)
        self.queue_scroll_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        self.log_panel = self.tab_view.tab("Logs")
        self.log_panel.grid_columnconfigure(0, weight=1); self.log_panel.grid_rowconfigure(0, weight=1)
        self.log_textbox = ctk.CTkTextbox(self.log_panel, state="disabled", text_color=AppTheme.COLOR_TEXT, font=AppTheme.FONT_MONO, fg_color=AppTheme.COLOR_BACKGROUND, corner_radius=10)
        self.log_textbox.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=2, column=0, sticky="sew", padx=(10,0), pady=5)
        action_frame.grid_columnconfigure((0, 1), weight=1)
        self.add_button = AnimatedButton(action_frame, text="Ajouter à la file d'attente", command=self.controller.add_to_queue, height=40, font=AppTheme.FONT_BOLD, fg_color=AppTheme.COLOR_ACCENT, hover_color=AppTheme.COLOR_ACCENT_HOVER)
        self.add_button.grid(row=0, column=0, padx=5, sticky="ew")
        self.start_button = AnimatedButton(action_frame, text="Démarrer la File", command=self.controller.start_queue, height=40, font=AppTheme.FONT_BOLD, fg_color=AppTheme.COLOR_SUCCESS, hover_color="#218838")
        self.start_button.grid(row=0, column=1, padx=5, sticky="ew")

        self.cancel_button = AnimatedButton(action_frame, text="Annuler le Rendu Actif", command=self.controller.cancel_ffmpeg, height=35, font=AppTheme.FONT_BOLD, fg_color=AppTheme.COLOR_ERROR, hover_color="#C82333")
        self.cancel_button.grid(row=1, column=0, columnspan=2, padx=5, pady=(5,0), sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(action_frame, orientation="horizontal", mode="determinate", height=10, fg_color=AppTheme.COLOR_FRAME_BORDER, progress_color=AppTheme.COLOR_ACCENT)
        self.progress_bar.grid(row=2, column=0, columnspan=2, padx=5, pady=(10, 0), sticky="ew")
        self.progress_bar.set(0)

        self.progress_label = ctk.CTkLabel(action_frame, text="0%", font=AppTheme.FONT_NORMAL, text_color=AppTheme.COLOR_TEXT)
        self.progress_label.grid(row=3, column=0, columnspan=2, padx=5, pady=(0, 5), sticky="ew")

    def show_task_frame(self, task_key):
        frame = self.task_frames.get(task_key)
        if frame:
            frame.appear()

    def on_drag_enter(self, event):
        self._dnd_target.config(bg="#2a2a2a")

    def on_drag_leave(self, event):
        self._dnd_target.config(bg=AppTheme.COLOR_BACKGROUND)

    def append_log(self, message, level="info"):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert(tk.END, message + "\n", (level,))
        self.log_textbox.tag_config("info", foreground=AppTheme.COLOR_TEXT)
        self.log_textbox.tag_config("success", foreground=AppTheme.COLOR_SUCCESS)
        self.log_textbox.tag_config("error", foreground=AppTheme.COLOR_ERROR)
        self.log_textbox.configure(state="disabled")
        self.log_textbox.see(tk.END)

# =============================================================================
# CONTRÔLEUR PRINCIPAL
# =============================================================================
class Controller:
    def __init__(self, root):
        self.root = root
        self.input_files = []
        self.job_queue_data = []
        self.is_processing = False
        self._current_proc = None # Added for ffmpeg process tracking
        self.current_job_total_duration = None # For progress tracking
        self.last_reported_progress = 0.0 # For smoothing progress updates
        self.total_jobs_in_queue = 0
        self.completed_jobs_count = 0

        self.sidebar = Sidebar(self.root, self, width=220)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.content_panel = ContentPanel(self.root, self)
        self.content_panel.grid(row=0, column=1, sticky="nsew")
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        self.sidebar.select_task("convert")
        self.update_ui_state()
        self.root.after(100, self.poll_ffmpeg_logs) # Start polling for ffmpeg logs

    def set_operation_mode(self, mode):
        self.operation_mode = mode
        self.content_panel.show_task_frame(mode)
        self.update_ui_state()

    def handle_drop(self, event: "tk.Event") -> None:
        files = self.root.tk.splitlist(event.data)
        valid_files = []
        invalid_files = []

        if self.operation_mode == "folders_to_videos":
            for f in files:
                path_str = f.strip('{').strip('}')
                path_obj = Path(path_str)
                if path_obj.is_dir():
                    valid_files.append(path_str)
                else:
                    invalid_files.append(path_str)
        else:
            for f in files:
                path_str = f.strip('{').strip('}')
                path_obj = Path(path_str)
                if path_obj.is_file():
                    valid_files.append(path_str)
                else:
                    invalid_files.append(path_str)

        for f in valid_files:
            self.add_file(f)

        if invalid_files:
            error_message = f"{len(invalid_files)} invalid file(s) were ignored:\n"
            if self.operation_mode == "folders_to_videos":
                error_message += "Only folders can be added in 'Folders -> Videos' mode.\n\n"
            else:
                error_message += "Only files can be added in this mode.\n\n"
            
            # Show a few examples
            for i, f in enumerate(invalid_files[:5]):
                error_message += f"- {Path(f).name}\n"
            if len(invalid_files) > 5:
                error_message += f"... and {len(invalid_files) - 5} more."
            
            messagebox.showwarning("Invalid Files", error_message)

    def add_file(self, filepath: str) -> None:
        if filepath not in self.input_files:
            is_folder = Path(filepath).is_dir()
            self.input_files.append(filepath)
            item = FileListItem(self.content_panel.file_scroll_frame, filepath, self.remove_file, is_folder=is_folder)
            item.pack(fill="x", padx=5, pady=2)
            self.update_ui_state()

    def remove_file(self, filepath: str, widget: ctk.CTkFrame) -> None:
        if filepath in self.input_files:
            self.input_files.remove(filepath)
        widget.destroy()
        self.update_ui_state()

    def add_to_queue(self) -> None:
        active_frame = self.content_panel.task_frames[self.operation_mode]
        settings = active_frame.get_settings()

        if self.operation_mode == "folders_to_videos":
            if not self.input_files:
                messagebox.showwarning("Attention", "Veuillez ajouter au moins un dossier.")
                return
            for folder_path in self.input_files:
                description = f"{self.sidebar.task_buttons[self.operation_mode].cget('text')} sur {Path(folder_path).name}"
                job_data = {"mode": self.operation_mode, "settings": settings, "input_files": [folder_path], "description": description}
                self.job_queue_data.append(job_data)
                item = QueueListItem(self.content_panel.queue_scroll_frame, job_data, self.remove_from_queue)
                item.pack(fill="x", padx=5, pady=2)
                logger.info(f"Ajouté: {description}")
                self.total_jobs_in_queue += 1 # Increment for each job
        else:
            if not self.input_files:
                messagebox.showwarning("Attention", "Veuillez ajouter au moins un fichier.")
                return
            description = f"{self.sidebar.task_buttons[self.operation_mode].cget('text')} sur {Path(self.input_files[0]).name}"
            job_data = {"mode": self.operation_mode, "settings": settings, "input_files": list(self.input_files), "description": description}
            self.job_queue_data.append(job_data)
            item = QueueListItem(self.content_panel.queue_scroll_frame, job_data, self.remove_from_queue)
            item.pack(fill="x", padx=5, pady=2)
            logger.info(f"Ajouté: {description}")
            self.total_jobs_in_queue += 1 # Increment for the single job
        self.update_ui_state()

    def remove_from_queue(self, job_data: dict, widget: ctk.CTkFrame) -> None:
        if job_data in self.job_queue_data:
            self.job_queue_data.remove(job_data)
        widget.destroy()
        self.update_ui_state()

    def start_queue(self) -> None:
        if self.is_processing or not self.job_queue_data:
            return
        self.is_processing = True
        self.total_jobs_in_queue = len(self.job_queue_data)
        self.completed_jobs_count = 0
        self.update_ui_state()
        logger.info("--- Démarrage de la file d'attente ---")
        self.process_next_job()

    def process_next_job(self) -> None:
        if not self.job_queue_data:
            self.is_processing = False
            self.root.after(0, self.update_ui_state)
            self.root.after(0, self.clear_queue_ui)
            logger.info("--- File d'attente terminée ---")
            return

        job = self.job_queue_data.pop(0)
        self.current_job_total_duration = None  # Reset for new job
        self.last_reported_progress = 0.0  # Reset for new job
        self.root.after(0, self.update_progress_ui, 0)  # Reset progress bar for current job

        try:
            logger.info(f"\nTraitement: {job['description']}")
            self.current_job_total_duration = self._get_job_total_duration(job)
            command, output_file = self._build_ffmpeg_command(job)
            if command:
                logger.info(f"Commande: {' '.join(command)}")
                start_ffmpeg_thread(command, on_finish=lambda success: self._on_ffmpeg_job_finished(job, success))
        except Exception as e:
            logger.exception(f"Erreur majeure lors du traitement de la tâche: {job['description']}")
            messagebox.showerror("Erreur", f"Une erreur majeure est survenue lors du traitement de la tâche: {job['description']}. Voir les logs pour plus de détails.")
            self.process_next_job()  # Process next job even if current one fails

    def clear_queue_ui(self) -> None:
        for widget in self.content_panel.queue_scroll_frame.winfo_children():
            widget.destroy()
        self.job_queue_data.clear()

    def _on_ffmpeg_job_finished(self, job: dict, success: bool) -> None:
        self.completed_jobs_count += 1
        if success:
            logger.info(f"SUCCÈS: {job['description']} terminé.")
            self.root.after(0, self.update_progress_ui, 1.0)  # Mark job as 100% complete
        else:
            logger.error(f"ERREUR: {job['description']} a échoué.")

        self.process_next_job()

    def _generate_ffmpeg_command_for_folder_images(self, folder_path: str, settings: dict) -> Tuple[List[str], str]:
        image_extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")
        images = sorted([f for f in Path(folder_path).iterdir() if f.is_file() and f.suffix.lower() in image_extensions])

        if not images:
            raise ValueError(f"Aucune image trouvée dans le dossier: {folder_path}")

        # Créer un fichier de liste temporaire pour le démultiplexeur concat de ffmpeg
        list_file_path = Path(tempfile.gettempdir()) / f"ffmpeg_imagelist_{os.getpid()}_{Path(folder_path).name}.txt"
        frame_duration = 1 / float(settings.get('framerate', 25))
        with open(list_file_path, "w", encoding="utf-8") as f:
            for img in images:
                f.write(f"file '{img.resolve()}'\n")
                f.write(f"duration {frame_duration}\n")
            f.write(f"file '{images[-1].resolve()}'\n")

        base_name = Path(folder_path).name
        output_file = str(Path(folder_path) / f"{base_name}_video.{settings['output_format']}")

        # --- Logique d'encodage optimisée ---
        pix_fmt = "yuv420p"
        if settings['output_format'] == 'webm':
            codec = 'libvpx-vp9'
            pix_fmt = 'yuva420p'
            codec_opts = {"-crf": str(settings['crf']), "-b:v": "0"}
        else:
            is_hevc = settings['output_format'] in ['mkv', 'mp4']
            codec, codec_opts = select_best_video_codec(settings['crf'], for_hevc=is_hevc)

        command = [
            FFMPEG_PATH,
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file_path),
            "-c:v", codec,
        ]
        for opt, val in codec_opts.items():
            command.extend([opt, val])
        command.extend([
            "-pix_fmt", pix_fmt,
            "-framerate", settings['framerate'], # Placé ici pour certains codecs
            output_file,
            "-y"
        ])
        # --- Fin de la logique ---
        
        return command, output_file

    def _get_video_duration(self, filepath: str) -> Optional[float]:
        if not FFPROBE_PATH:
            return None
        try:
            cmd = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
            duration = float(result.stdout.strip())
            return duration
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
            logger.error(f"Could not get duration for {filepath} using ffprobe: {e}")
            return None

    def _get_job_total_duration(self, job: dict) -> Optional[float]:
        mode, settings, inputs = job['mode'], job['settings'], job['input_files']
        if not inputs: return None

        if mode in ["convert", "extract_audio", "extract_image", "merge", "subtitles", "speed"]:
            return self._get_video_duration(inputs[0])
        elif mode in ["img_seq", "folders_to_videos"]:
            # For image sequences, estimate duration based on number of images and framerate
            framerate = float(settings.get('framerate', 25)) # Default to 25 if not set
            if framerate == 0: return None

            image_count = 0
            if mode == "img_seq":
                # For img_seq, inputs is a list of image files
                image_count = len(inputs)
            elif mode == "folders_to_videos":
                # For folders_to_videos, inputs[0] is a folder path
                folder_path = inputs[0]
                image_extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")
                image_count = len([f for f in Path(folder_path).iterdir() if f.is_file() and f.suffix.lower() in image_extensions])

            if image_count > 0:
                return image_count / framerate
        return None

    def _build_ffmpeg_command(self, job: dict) -> Tuple[List[str], str]:
        mode, settings, inputs = job['mode'], job['settings'], job['input_files']
        if mode == "folders_to_videos":
            if not inputs: raise ValueError("Aucun dossier spécifié pour la conversion.")
            return self._generate_ffmpeg_command_for_folder_images(inputs[0], settings)
        else:
            return _generate_ffmpeg_command_and_output(mode, settings, inputs, self.find_image_sequence_pattern)

    def find_image_sequence_pattern(self, files: List[str]) -> Optional[dict]:
        if not files or len(files) < 2: return None
        files.sort()
        first_name = Path(files[0]).stem
        ext = Path(files[0]).suffix
        match = re.search(r'(\d+)$', first_name)
        if not match: return None
        padding = len(match.group(1))
        prefix = first_name[:match.start(1)]
        start_number = int(match.group(1))
        ffmpeg_pattern = f"{prefix}%0{padding}d{ext}"
        return {"pattern": str(Path(files[0]).parent / ffmpeg_pattern), "start": start_number, "prefix": prefix}





    def update_ui_state(self):
        has_files = bool(self.input_files)
        has_queue = bool(self.job_queue_data)
        add_btn_state = "normal" if has_files and not self.is_processing else "disabled"
        start_btn_state = "normal" if has_queue and not self.is_processing else "disabled"
        cancel_btn_state = "normal" if self.is_processing else "disabled"

        self.content_panel.add_button.configure(state=add_btn_state)
        self.content_panel.start_button.configure(state=start_btn_state)
        self.content_panel.cancel_button.configure(state=cancel_btn_state)

        if self.is_processing:
            self.content_panel.start_button.configure(text="Traitement en cours...")
        else:
            self.content_panel.start_button.configure(text="Démarrer la File")

    def update_progress_ui(self, current_job_progress: float, text: str = ""):
        if self.total_jobs_in_queue > 0:
            global_progress = (self.completed_jobs_count + current_job_progress) / self.total_jobs_in_queue
        else:
            global_progress = 0.0

        self.content_panel.progress_bar.set(global_progress)
        if text:
            self.content_panel.progress_label.configure(text=text)
        else:
            self.content_panel.progress_label.configure(text=f"{int(global_progress * 100)}% (Tâche {self.completed_jobs_count + 1}/{self.total_jobs_in_queue})")

    def poll_ffmpeg_logs(self) -> None:
        try:
            while True:
                typ, payload = _ffmpeg_log_queue.get_nowait()
                if typ == "line":
                    self.content_panel.append_log(payload)  # méthode qui affiche dans la zone log
                    # Parse FFmpeg progress
                    if self.current_job_total_duration:
                        time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', payload)
                        if time_match:
                            h, m, s = map(float, time_match.groups())
                            current_time = h * 3600 + m * 60 + s
                            progress = min(current_time / self.current_job_total_duration, 1.0)
                            if progress > self.last_reported_progress:
                                self.update_progress_ui(progress)
                                self.last_reported_progress = progress
                elif typ == "error":
                    self.content_panel.append_log(payload, "error")
                    logger.error(payload) # Log to file as well
                elif typ == "proc":
                    self._current_proc = payload
                elif typ == "done":
                    self._current_proc = None
        except queue.Empty:
            pass
        self.root.after(100, self.poll_ffmpeg_logs)  # relancer toutes les 100ms



    def cancel_ffmpeg(self) -> None:
        if hasattr(self, "_current_proc") and self._current_proc:
            try:
                self._current_proc.terminate()
                logger.info("Opération FFmpeg annulée.")
            except Exception as e:
                logger.exception(f"Erreur lors de l'annulation de l'opération FFmpeg: {e}")
                messagebox.showerror("Erreur d'annulation", f"Une erreur est survenue lors de l'annulation de l'opération FFmpeg. Voir les logs pour plus de détails.")
            finally:
                self._current_proc = None
                self.is_processing = False
                self.update_ui_state()

# =============================================================================
# APPLICATION ROOT
# =============================================================================
def get_application_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(__file__)

class App(TkinterDnD.Tk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if sys.platform.startswith("win"):
            from ctypes import windll
            windll.shell32.SetCurrentProcessExplicitAppUserModelID("ffmpeg_gui.ffmpeg_gui.1.0")

        self.title("FFmpeg Studio")
        self.geometry("1200x750")
        self.minsize(900, 600)
        self.configure(background=AppTheme.COLOR_BACKGROUND)
        self.root_controller = Controller(self)

        # Set the application icon (requires .ico file on Windows)
        icon_path = os.path.join(get_application_path(), "app_icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)
        else:
            logger.warning(f"Icon file not found at {icon_path}. Please provide an app_icon.ico file.")

        if not FFMPEG_PATH:
            messagebox.showerror("Erreur", "ffmpeg n'est pas installé ou n'est pas dans le PATH.")
            logger.error("ffmpeg non trouvé ou non accessible dans le PATH.")
            self.destroy() # Close the application if ffmpeg is not found

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    window = App()
    window.mainloop()
