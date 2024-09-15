import os
import sys
import time
import torch
import librosa
import logging
import traceback
import numpy as np
import soundfile as sf
import noisereduce as nr
from pedalboard import (
    Pedalboard,
    Chorus,
    Distortion,
    Reverb,
    PitchShift,
    Limiter,
    Gain,
    Bitcrush,
    Clipping,
    Compressor,
    Delay,
)

from scipy.io import wavfile
from audio_upscaler import upscale

now_dir = os.getcwd()
sys.path.append(now_dir)

from rvc.infer.pipeline import Pipeline as VC
from rvc.lib.utils import load_audio_infer, load_embedding
from rvc.lib.tools.split_audio import process_audio, merge_audio
from rvc.lib.algorithm.synthesizers import Synthesizer
from rvc.configs.config import Config

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("faiss").setLevel(logging.WARNING)
logging.getLogger("faiss.loader").setLevel(logging.WARNING)


class VoiceConverter:
    """
    A class for performing voice conversion using the Retrieval-Based Voice Conversion (RVC) method.
    """

    def __init__(self):
        """
        Initializes the VoiceConverter with default configuration, and sets up models and parameters.
        """
        self.config = Config()  # Load RVC configuration
        self.hubert_model = (
            None  # Initialize the Hubert model (for embedding extraction)
        )
        self.last_embedder_model = None  # Last used embedder model
        self.tgt_sr = None  # Target sampling rate for the output audio
        self.net_g = None  # Generator network for voice conversion
        self.vc = None  # Voice conversion pipeline instance
        self.cpt = None  # Checkpoint for loading model weights
        self.version = None  # Model version
        self.n_spk = None  # Number of speakers in the model
        self.use_f0 = None  # Whether the model uses F0

    def load_hubert(self, embedder_model: str, embedder_model_custom: str = None):
        """
        Loads the HuBERT model for speaker embedding extraction.

        Args:
            embedder_model (str): Path to the pre-trained HuBERT model.
            embedder_model_custom (str): Path to the custom HuBERT model.
        """
        self.hubert_model = load_embedding(embedder_model, embedder_model_custom)
        self.hubert_model.to(self.config.device)
        self.hubert_model = (
            self.hubert_model.half()
            if self.config.is_half
            else self.hubert_model.float()
        )
        self.hubert_model.eval()

    @staticmethod
    def remove_audio_noise(input_audio_path, reduction_strength=0.7):
        """
        Removes noise from an audio file using the NoiseReduce library.

        Args:
            input_audio_path (str): Path to the input audio file.
            reduction_strength (float): Strength of the noise reduction. Default is 0.7.
        """
        try:
            rate, data = wavfile.read(input_audio_path)
            reduced_noise = nr.reduce_noise(
                y=data, sr=rate, prop_decrease=reduction_strength
            )
            return reduced_noise
        except Exception as error:
            print(f"An error occurred removing audio noise: {error}")
            return None

    @staticmethod
    def convert_audio_format(input_path, output_path, output_format):
        """
        Converts an audio file to a specified output format.

        Args:
            input_path (str): Path to the input audio file.
            output_path (str): Path to the output audio file.
            output_format (str): Desired audio format (e.g., "WAV", "MP3").
        """
        try:
            if output_format != "WAV":
                print(f"Converting audio to {output_format} format...")
                audio, sample_rate = librosa.load(input_path, sr=None)
                common_sample_rates = [
                    8000,
                    11025,
                    12000,
                    16000,
                    22050,
                    24000,
                    32000,
                    44100,
                    48000,
                ]
                target_sr = min(common_sample_rates, key=lambda x: abs(x - sample_rate))
                audio = librosa.resample(
                    audio, orig_sr=sample_rate, target_sr=target_sr
                )
                sf.write(output_path, audio, target_sr, format=output_format.lower())
            return output_path
        except Exception as error:
            print(f"An error occurred converting the audio format: {error}")

    @staticmethod
    def post_process_audio(
        audio_input,
        sample_rate,
        reverb: bool,
        reverb_room_size: float,
        reverb_damping: float,
        reverb_wet_level: float,
        reverb_dry_level: float,
        reverb_width: float,
        reverb_freeze_mode: float,
        pitch_shift: bool,
        pitch_shift_semitones: int,
        limiter: bool,
        limiter_threshold: float,
        limiter_release: float,
        gain: bool,
        gain_db: float,
        distortion: bool,
        distortion_gain: float,
        chorus: bool,
        chorus_rate: float,
        chorus_depth: float,
        chorus_delay: float,
        chorus_feedback: float,
        chorus_mix: float,
        bitcrush: bool,
        bitcrush_bit_depth: int,
        clipping: bool,
        clipping_threshold: float,
        compressor: bool,
        compressor_threshold: float,
        compressor_ratio: float,
        compressor_attack: float,
        compressor_release: float,
        delay: bool,
        delay_seconds: float,
        delay_feedback: float,
        delay_mix: float,
        audio_output_path: str,
    ):
        board = Pedalboard()
        if reverb:
            reverb = Reverb(
                room_size=reverb_room_size,
                damping=reverb_damping,
                wet_level=reverb_wet_level,
                dry_level=reverb_dry_level,
                width=reverb_width,
                freeze_mode=reverb_freeze_mode,
            )
            board.append(reverb)
        if pitch_shift:
            pitch_shift = PitchShift(semitones=pitch_shift_semitones)
            board.append(pitch_shift)
        if limiter:
            limiter = Limiter(
                threshold_db=limiter_threshold, release_ms=limiter_release
            )
            board.append(limiter)
        if gain:
            gain = Gain(gain_db=gain_db)
            board.append(gain)
        if distortion:
            distortion = Distortion(drive_db=distortion_gain)
            board.append(distortion)
        if chorus:
            chorus = Chorus(
                rate_hz=chorus_rate,
                depth=chorus_depth,
                centre_delay_ms=chorus_delay,
                feedback=chorus_feedback,
                mix=chorus_mix,
            )
            board.append(chorus)
        if bitcrush:
            bitcrush = Bitcrush(bit_depth=bitcrush_bit_depth)
            board.append(bitcrush)
        if clipping:
            clipping = Clipping(threshold_db=clipping_threshold)
            board.append(clipping)
        if compressor:
            compressor = Compressor(
                threshold_db=compressor_threshold,
                ratio=compressor_ratio,
                attack_ms=compressor_attack,
                release_ms=compressor_release,
            )
            board.append(compressor)
        if delay:
            delay = Delay(
                delay_seconds=delay_seconds,
                feedback=delay_feedback,
                mix=delay_mix,
            )
            board.append(delay)
        audio_input, sample_rate = librosa.load(audio_input, sr=sample_rate)
        output = board(audio_input, sample_rate)
        sf.write(audio_output_path, output, sample_rate, format="WAV")
        return audio_output_path

    def convert_audio(
        self,
        audio_input_path: str,
        audio_output_path: str,
        model_path: str,
        index_path: str,
        embedder_model: str,
        pitch: int,
        f0_file: str,
        f0_method: str,
        index_rate: float,
        volume_envelope: int,
        protect: float,
        hop_length: int,
        split_audio: bool,
        f0_autotune: bool,
        filter_radius: int,
        embedder_model_custom: str,
        clean_audio: bool,
        clean_strength: float,
        export_format: str,
        upscale_audio: bool,
        formant_shifting: bool,
        formant_qfrency: float,
        formant_timbre: float,
        post_process: bool,
        reverb: bool,
        pitch_shift: bool,
        limiter: bool,
        gain: bool,
        distortion: bool,
        chorus: bool,
        bitcrush: bool,
        clipping: bool,
        compressor: bool,
        delay: bool,
        sliders: dict,
        resample_sr: int = 0,
        sid: int = 0,
    ):
        """
        Performs voice conversion on the input audio.

        Args:
            audio_input_path (str): Path to the input audio file.
            audio_output_path (str): Path to the output audio file.
            model_path (str): Path to the voice conversion model.
            index_path (str): Path to the index file.
            sid (int, optional): Speaker ID. Default is 0.
            pitch (str, optional): Key for F0 up-sampling. Default is None.
            f0_file (str, optional): Path to the F0 file. Default is None.
            f0_method (str, optional): Method for F0 extraction. Default is None.
            index_rate (float, optional): Rate for index matching. Default is None.
            resample_sr (int, optional): Resample sampling rate. Default is 0.
            volume_envelope (float, optional): RMS mix rate. Default is None.
            protect (float, optional): Protection rate for certain audio segments. Default is None.
            hop_length (int, optional): Hop length for audio processing. Default is None.
            split_audio (bool, optional): Whether to split the audio for processing. Default is False.
            f0_autotune (bool, optional): Whether to use F0 autotune. Default is False.
            filter_radius (int, optional): Radius for filtering. Default is None.
            embedder_model (str, optional): Path to the embedder model. Default is None.
            embedder_model_custom (str, optional): Path to the custom embedder model. Default is None.
            clean_audio (bool, optional): Whether to clean the audio. Default is False.
            clean_strength (float, optional): Strength of the audio cleaning. Default is 0.7.
            export_format (str, optional): Format for exporting the audio. Default is "WAV".
            upscale_audio (bool, optional): Whether to upscale the audio. Default is False.
            formant_shift (bool, optional): Whether to shift the formants. Default is False.
            formant_qfrency (float, optional): Formant frequency. Default is 1.0.
            formant_timbre (float, optional): Formant timbre. Default is 1.0.
            reverb (bool, optional): Whether to apply reverb. Default is False.
            pitch_shift (bool, optional): Whether to apply pitch shift. Default is False.
            limiter (bool, optional): Whether to apply a limiter. Default is False.
            gain (bool, optional): Whether to apply gain. Default is False.
            distortion (bool, optional): Whether to apply distortion. Default is False.
            chorus (bool, optional): Whether to apply chorus. Default is False.
            bitcrush (bool, optional): Whether to apply bitcrush. Default is False.
            clipping (bool, optional): Whether to apply clipping. Default is False.
            compressor (bool, optional): Whether to apply a compressor. Default is False.
            delay (bool, optional): Whether to apply delay. Default is False.
            sliders (dict, optional): Dictionary of effect parameters. Default is None.
        """
        self.get_vc(model_path, sid)

        try:
            start_time = time.time()
            print(f"Converting audio '{audio_input_path}'...")

            if upscale_audio == True:
                upscale(audio_input_path, audio_input_path)
            audio = load_audio_infer(
                audio_input_path,
                16000,
                formant_shifting,
                formant_qfrency,
                formant_timbre,
            )
            audio_max = np.abs(audio).max() / 0.95

            if audio_max > 1:
                audio /= audio_max

            if not self.hubert_model or embedder_model != self.last_embedder_model:
                self.load_hubert(embedder_model, embedder_model_custom)
                self.last_embedder_model = embedder_model

            file_index = (
                index_path.strip()
                .strip('"')
                .strip("\n")
                .strip('"')
                .strip()
                .replace("trained", "added")
            )

            if self.tgt_sr != resample_sr >= 16000:
                self.tgt_sr = resample_sr

            if split_audio:
                result, new_dir_path = process_audio(audio_input_path)
                if result == "Error":
                    return "Error with Split Audio", None

                dir_path = (
                    new_dir_path.strip().strip('"').strip("\n").strip('"').strip()
                )
                if dir_path:
                    paths = [
                        os.path.join(root, name)
                        for root, _, files in os.walk(dir_path, topdown=False)
                        for name in files
                        if name.endswith(".wav") and root == dir_path
                    ]
                try:
                    for path in paths:
                        self.convert_audio(
                            audio_input_path=path,
                            audio_output_path=path,
                            model_path=model_path,
                            index_path=index_path,
                            sid=sid,
                            pitch=pitch,
                            f0_file=None,
                            f0_method=f0_method,
                            index_rate=index_rate,
                            resample_sr=resample_sr,
                            volume_envelope=volume_envelope,
                            protect=protect,
                            hop_length=hop_length,
                            split_audio=False,
                            f0_autotune=f0_autotune,
                            filter_radius=filter_radius,
                            export_format=export_format,
                            upscale_audio=upscale_audio,
                            embedder_model=embedder_model,
                            embedder_model_custom=embedder_model_custom,
                            clean_audio=clean_audio,
                            clean_strength=clean_strength,
                            formant_shifting=formant_shifting,
                            formant_qfrency=formant_qfrency,
                            formant_timbre=formant_timbre,
                            post_process=post_process,
                            reverb=reverb,
                            pitch_shift=pitch_shift,
                            limiter=limiter,
                            gain=gain,
                            distortion=distortion,
                            chorus=chorus,
                            bitcrush=bitcrush,
                            clipping=clipping,
                            compressor=compressor,
                            delay=delay,
                            sliders=sliders,
                        )
                except Exception as error:
                    print(f"An error occurred processing the segmented audio: {error}")
                    print(traceback.format_exc())
                    return f"Error {error}"
                print("Finished processing segmented audio, now merging audio...")
                merge_timestamps_file = os.path.join(
                    os.path.dirname(new_dir_path),
                    f"{os.path.basename(audio_input_path).split('.')[0]}_timestamps.txt",
                )
                self.tgt_sr, audio_opt = merge_audio(merge_timestamps_file)
                os.remove(merge_timestamps_file)
                if post_process:
                    audio_opt = self.post_process_audio(
                        audio_input=audio_opt,
                        sample_rate=self.tgt_sr,
                        reverb=reverb,
                        reverb_room_size=sliders[0],
                        reverb_damping=sliders[1],
                        reverb_wet_level=sliders[2],
                        reverb_dry_level=sliders[3],
                        reverb_width=sliders[4],
                        reverb_freeze_mode=sliders[5],
                        pitch_shift=pitch_shift,
                        pitch_shift_semitones=sliders[6],
                        limiter=limiter,
                        limiter_threshold=sliders[7],
                        limiter_release=sliders[8],
                        gain=gain,
                        gain_db=sliders[9],
                        distortion=distortion,
                        distortion_gain=sliders[10],
                        chorus=chorus,
                        chorus_rate=sliders[11],
                        chorus_depth=sliders[12],
                        chorus_delay=sliders[13],
                        chorus_feedback=sliders[14],
                        chorus_mix=sliders[15],
                        bitcrush=bitcrush,
                        bitcrush_bit_depth=sliders[16],
                        clipping=clipping,
                        clipping_threshold=sliders[17],
                        compressor=compressor,
                        compressor_threshold=sliders[18],
                        compressor_ratio=sliders[19],
                        compressor_attack=sliders[20],
                        compressor_release=sliders[21],
                        delay=delay,
                        delay_seconds=sliders[22],
                        delay_feedback=sliders[23],
                        delay_mix=sliders[24],
                        audio_output_path=audio_output_path,
                    )
                sf.write(audio_output_path, audio_opt, self.tgt_sr, format="WAV")
            else:
                audio_opt = self.vc.pipeline(
                    model=self.hubert_model,
                    net_g=self.net_g,
                    sid=sid,
                    audio=audio,
                    input_audio_path=audio_input_path,
                    pitch=pitch,
                    f0_method=f0_method,
                    file_index=file_index,
                    index_rate=index_rate,
                    pitch_guidance=self.use_f0,
                    filter_radius=filter_radius,
                    tgt_sr=self.tgt_sr,
                    resample_sr=resample_sr,
                    volume_envelope=volume_envelope,
                    version=self.version,
                    protect=protect,
                    hop_length=hop_length,
                    f0_autotune=f0_autotune,
                    f0_file=f0_file,
                )

            if audio_output_path:
                sf.write(audio_output_path, audio_opt, self.tgt_sr, format="WAV")

            if clean_audio:
                cleaned_audio = self.remove_audio_noise(
                    audio_output_path, clean_strength
                )
                if cleaned_audio is not None:
                    sf.write(
                        audio_output_path, cleaned_audio, self.tgt_sr, format="WAV"
                    )
            if post_process:
                audio_output_path = self.post_process_audio(
                    audio_input=audio_output_path,
                    sample_rate=self.tgt_sr,
                    reverb=reverb,
                    reverb_room_size=sliders["reverb_room_size"],
                    reverb_damping=sliders["reverb_damping"],
                    reverb_wet_level=sliders["reverb_wet_level"],
                    reverb_dry_level=sliders["reverb_dry_level"],
                    reverb_width=sliders["reverb_width"],
                    reverb_freeze_mode=sliders["reverb_freeze_mode"],
                    pitch_shift=pitch_shift,
                    pitch_shift_semitones=sliders["pitch_shift_semitones"],
                    limiter=limiter,
                    limiter_threshold=sliders["limiter_threshold"],
                    limiter_release=sliders["limiter_release"],
                    gain=gain,
                    gain_db=sliders["gain_db"],
                    distortion=distortion,
                    distortion_gain=sliders["distortion_gain"],
                    chorus=chorus,
                    chorus_rate=sliders["chorus_rate"],
                    chorus_depth=sliders["chorus_depth"],
                    chorus_delay=sliders["chorus_delay"],
                    chorus_feedback=sliders["chorus_feedback"],
                    chorus_mix=sliders["chorus_mix"],
                    bitcrush=bitcrush,
                    bitcrush_bit_depth=sliders["bitcrush_bit_depth"],
                    clipping=clipping,
                    clipping_threshold=sliders["clipping_threshold"],
                    compressor=compressor,
                    compressor_threshold=sliders["compressor_threshold"],
                    compressor_ratio=sliders["compressor_ratio"],
                    compressor_attack=sliders["compressor_attack"],
                    compressor_release=sliders["compressor_release"],
                    delay=delay,
                    delay_seconds=sliders["delay_seconds"],
                    delay_feedback=sliders["delay_feedback"],
                    delay_mix=sliders["delay_mix"],
                    audio_output_path=audio_output_path,
                )
            output_path_format = audio_output_path.replace(
                ".wav", f".{export_format.lower()}"
            )
            audio_output_path = self.convert_audio_format(
                audio_output_path, output_path_format, export_format
            )

            elapsed_time = time.time() - start_time
            print(
                f"Conversion completed at '{audio_output_path}' in {elapsed_time:.2f} seconds."
            )

        except Exception as error:
            print(f"An error occurred during audio conversion: {error}")
            print(traceback.format_exc())

    def convert_audio_batch(
        self,
        audio_input_paths: str,
        audio_output_path: str,
        model_path: str,
        index_path: str,
        embedder_model: str,
        pitch: int,
        f0_file: str,
        f0_method: str,
        index_rate: float,
        volume_envelope: int,
        protect: float,
        hop_length: int,
        split_audio: bool,
        f0_autotune: bool,
        filter_radius: int,
        embedder_model_custom: str,
        clean_audio: bool,
        clean_strength: float,
        export_format: str,
        upscale_audio: bool,
        formant_shifting: bool,
        formant_qfrency: float,
        formant_timbre: float,
        resample_sr: int = 0,
        sid: int = 0,
        pid_file_path: str = None,
        post_process: bool = False,
        reverb: bool = False,
        pitch_shift: bool = False,
        limiter: bool = False,
        gain: bool = False,
        distortion: bool = False,
        chorus: bool = False,
        bitcrush: bool = False,
        clipping: bool = False,
        compressor: bool = False,
        delay: bool = False,
        sliders: dict = None,
    ):
        """
        Performs voice conversion on a batch of input audio files.

        Args:
            audio_input_paths (list): List of paths to the input audio files.
            audio_output_path (str): Path to the output audio file.
            model_path (str): Path to the voice conversion model.
            index_path (str): Path to the index file.
            sid (int, optional): Speaker ID. Default is 0.
            pitch (str, optional): Key for F0 up-sampling. Default is None.
            f0_file (str, optional): Path to the F0 file. Default is None.
            f0_method (str, optional): Method for F0 extraction. Default is None.
            index_rate (float, optional): Rate for index matching. Default is None.
            resample_sr (int, optional): Resample sampling rate. Default is 0.
            volume_envelope (float, optional): RMS mix rate. Default is None.
            protect (float, optional): Protection rate for certain audio segments. Default is None.
            hop_length (int, optional): Hop length for audio processing. Default is None.
            split_audio (bool, optional): Whether to split the audio for processing. Default is False.
            f0_autotune (bool, optional): Whether to use F0 autotune. Default is False.
            filter_radius (int, optional): Radius for filtering. Default is None.
            embedder_model (str, optional): Path to the embedder model. Default is None.
            embedder_model_custom (str, optional): Path to the custom embedder model. Default is None.
            clean_audio (bool, optional): Whether to clean the audio. Default is False.
            clean_strength (float, optional): Strength of the audio cleaning. Default is 0.7.
            export_format (str, optional): Format for exporting the audio. Default is "WAV".
            upscale_audio (bool, optional): Whether to upscale the audio. Default is False.
            formant_shift (bool, optional): Whether to shift the formants. Default is False.
            formant_qfrency (float, optional): Formant frequency. Default is 1.0.
            formant_timbre (float, optional): Formant timbre. Default is 1.0.
            pid_file_path (str, optional): Path to the PID file. Default is None.
            post_process (bool, optional): Whether to apply post-processing effects. Default is False.
            reverb (bool, optional): Whether to apply reverb. Default is False.
            pitch_shift (bool, optional): Whether to apply pitch shift. Default is False.
            limiter (bool, optional): Whether to apply a limiter. Default is False.
            gain (bool, optional): Whether to apply gain. Default is False.
            distortion (bool, optional): Whether to apply distortion. Default is False.
            chorus (bool, optional): Whether to apply chorus. Default is False.
            bitcrush (bool, optional): Whether to apply bitcrush. Default is False.
            clipping (bool, optional): Whether to apply clipping. Default is False.
            compressor (bool, optional): Whether to apply a compressor. Default is False.
            delay (bool, optional): Whether to apply delay. Default is False.
            sliders (dict, optional): Dictionary of effect parameters. Default is None.

        """
        pid = os.getpid()
        with open(pid_file_path, "w") as pid_file:
            pid_file.write(str(pid))
        try:
            if not self.hubert_model or embedder_model != self.last_embedder_model:
                self.load_hubert(embedder_model, embedder_model_custom)
                self.last_embedder_model = embedder_model
            self.get_vc(model_path, sid)
            file_index = (
                index_path.strip()
                .strip('"')
                .strip("\n")
                .strip('"')
                .strip()
                .replace("trained", "added")
            )
            start_time = time.time()
            print(f"Converting audio batch '{audio_input_paths}'...")
            audio_files = [
                f
                for f in os.listdir(audio_input_paths)
                if f.endswith((".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"))
            ]
            print(f"Detected {len(audio_files)} audio files for inference.")
            for i, audio_input_path in enumerate(audio_files):
                audio_output_paths = os.path.join(
                    audio_output_path,
                    f"{os.path.splitext(os.path.basename(audio_input_path))[0]}_output.{export_format.lower()}",
                )
                if os.path.exists(audio_output_paths):
                    continue
                print(f"Converting audio '{audio_input_path}'...")
                audio_input_path = os.path.join(audio_input_paths, audio_input_path)

                if upscale_audio == True:
                    upscale(audio_input_path, audio_input_path)
                audio = load_audio_infer(
                    audio_input_path,
                    16000,
                    formant_shifting,
                    formant_qfrency,
                    formant_timbre,
                )
                audio_max = np.abs(audio).max() / 0.95

                if audio_max > 1:
                    audio /= audio_max

                if self.tgt_sr != resample_sr >= 16000:
                    self.tgt_sr = resample_sr

                if split_audio:
                    result, new_dir_path = process_audio(audio_input_path)
                    if result == "Error":
                        return "Error with Split Audio", None

                    dir_path = (
                        new_dir_path.strip().strip('"').strip("\n").strip('"').strip()
                    )
                    if dir_path:
                        paths = [
                            os.path.join(root, name)
                            for root, _, files in os.walk(dir_path, topdown=False)
                            for name in files
                            if name.endswith(".wav") and root == dir_path
                        ]
                    try:
                        for path in paths:
                            self.convert_audio(
                                audio_input_path=path,
                                audio_output_path=path,
                                model_path=model_path,
                                index_path=index_path,
                                sid=sid,
                                pitch=pitch,
                                f0_file=None,
                                f0_method=f0_method,
                                index_rate=index_rate,
                                resample_sr=resample_sr,
                                volume_envelope=volume_envelope,
                                protect=protect,
                                hop_length=hop_length,
                                split_audio=False,
                                f0_autotune=f0_autotune,
                                filter_radius=filter_radius,
                                export_format=export_format,
                                upscale_audio=upscale_audio,
                                embedder_model=embedder_model,
                                embedder_model_custom=embedder_model_custom,
                                clean_audio=clean_audio,
                                clean_strength=clean_strength,
                                formant_shifting=formant_shifting,
                                formant_qfrency=formant_qfrency,
                                formant_timbre=formant_timbre,
                                post_process=post_process,
                                reverb=reverb,
                                pitch_shift=pitch_shift,
                                limiter=limiter,
                                gain=gain,
                                distortion=distortion,
                                chorus=chorus,
                                bitcrush=bitcrush,
                                clipping=clipping,
                                compressor=compressor,
                                delay=delay,
                                sliders=sliders,
                            )
                    except Exception as error:
                        print(
                            f"An error occurred processing the segmented audio: {error}"
                        )
                        print(traceback.format_exc())
                        return f"Error {error}"
                    print("Finished processing segmented audio, now merging audio...")
                    merge_timestamps_file = os.path.join(
                        os.path.dirname(new_dir_path),
                        f"{os.path.basename(audio_input_path).split('.')[0]}_timestamps.txt",
                    )
                    self.tgt_sr, audio_opt = merge_audio(merge_timestamps_file)
                    os.remove(merge_timestamps_file)
                    if post_process:
                        audio_opt = self.post_process_audio(
                            audio_input=audio_opt,
                            sample_rate=self.tgt_sr,
                            reverb=reverb,
                            reverb_room_size=sliders[0],
                            reverb_damping=sliders[1],
                            reverb_wet_level=sliders[2],
                            reverb_dry_level=sliders[3],
                            reverb_width=sliders[4],
                            reverb_freeze_mode=sliders[5],
                            pitch_shift=pitch_shift,
                            pitch_shift_semitones=sliders[6],
                            limiter=limiter,
                            limiter_threshold=sliders[7],
                            limiter_release=sliders[8],
                            gain=gain,
                            gain_db=sliders[9],
                            distortion=distortion,
                            distortion_gain=sliders[10],
                            chorus=chorus,
                            chorus_rate=sliders[11],
                            chorus_depth=sliders[12],
                            chorus_delay=sliders[13],
                            chorus_feedback=sliders[14],
                            chorus_mix=sliders[15],
                            bitcrush=bitcrush,
                            bitcrush_bit_depth=sliders[16],
                            clipping=clipping,
                            clipping_threshold=sliders[17],
                            compressor=compressor,
                            compressor_threshold=sliders[18],
                            compressor_ratio=sliders[19],
                            compressor_attack=sliders[20],
                            compressor_release=sliders[21],
                            delay=delay,
                            delay_seconds=sliders[22],
                            delay_feedback=sliders[23],
                            delay_mix=sliders[24],
                            audio_output_path=audio_output_paths,
                        )
                        sf.write(
                            audio_output_paths, audio_opt, self.tgt_sr, format="WAV"
                        )
                else:
                    audio_opt = self.vc.pipeline(
                        model=self.hubert_model,
                        net_g=self.net_g,
                        sid=sid,
                        audio=audio,
                        input_audio_path=audio_input_path,
                        pitch=pitch,
                        f0_method=f0_method,
                        file_index=file_index,
                        index_rate=index_rate,
                        pitch_guidance=self.use_f0,
                        filter_radius=filter_radius,
                        tgt_sr=self.tgt_sr,
                        resample_sr=resample_sr,
                        volume_envelope=volume_envelope,
                        version=self.version,
                        protect=protect,
                        hop_length=hop_length,
                        f0_autotune=f0_autotune,
                        f0_file=f0_file,
                    )

                if audio_output_paths:
                    sf.write(audio_output_paths, audio_opt, self.tgt_sr, format="WAV")

                if clean_audio:
                    cleaned_audio = self.remove_audio_noise(
                        audio_output_paths, clean_strength
                    )
                    if cleaned_audio is not None:
                        sf.write(
                            audio_output_paths, cleaned_audio, self.tgt_sr, format="WAV"
                        )
                if post_process:
                    audio_output_paths = self.post_process_audio(
                        audio_input=audio_output_paths,
                        sample_rate=self.tgt_sr,
                        reverb=reverb,
                        reverb_room_size=sliders["reverb_room_size"],
                        reverb_damping=sliders["reverb_damping"],
                        reverb_wet_level=sliders["reverb_wet_level"],
                        reverb_dry_level=sliders["reverb_dry_level"],
                        reverb_width=sliders["reverb_width"],
                        reverb_freeze_mode=sliders["reverb_freeze_mode"],
                        pitch_shift=pitch_shift,
                        pitch_shift_semitones=sliders["pitch_shift_semitones"],
                        limiter=limiter,
                        limiter_threshold=sliders["limiter_threshold"],
                        limiter_release=sliders["limiter_release"],
                        gain=gain,
                        gain_db=sliders["gain_db"],
                        distortion=distortion,
                        distortion_gain=sliders["distortion_gain"],
                        chorus=chorus,
                        chorus_rate=sliders["chorus_rate"],
                        chorus_depth=sliders["chorus_depth"],
                        chorus_delay=sliders["chorus_delay"],
                        chorus_feedback=sliders["chorus_feedback"],
                        chorus_mix=sliders["chorus_mix"],
                        bitcrush=bitcrush,
                        bitcrush_bit_depth=sliders["bitcrush_bit_depth"],
                        clipping=clipping,
                        clipping_threshold=sliders["clipping_threshold"],
                        compressor=compressor,
                        compressor_threshold=sliders["compressor_threshold"],
                        compressor_ratio=sliders["compressor_ratio"],
                        compressor_attack=sliders["compressor_attack"],
                        compressor_release=sliders["compressor_release"],
                        delay=delay,
                        delay_seconds=sliders["delay_seconds"],
                        delay_feedback=sliders["delay_feedback"],
                        delay_mix=sliders["delay_mix"],
                        audio_output_path=audio_output_paths,
                    )
                output_path_format = audio_output_paths.replace(
                    ".wav", f".{export_format.lower()}"
                )
                audio_output_paths = self.convert_audio_format(
                    audio_output_paths, output_path_format, export_format
                )
                print(f"Conversion completed at '{audio_output_paths}'.")
            elapsed_time = time.time() - start_time
            print(f"Batch conversion completed in {elapsed_time:.2f} seconds.")
            os.remove(pid_file_path)
        except Exception as error:
            print(f"An error occurred during audio conversion: {error}")
            print(traceback.format_exc())

    def get_vc(self, weight_root, sid):
        """
        Loads the voice conversion model and sets up the pipeline.

        Args:
            weight_root (str): Path to the model weights.
            sid (int): Speaker ID.
        """
        if sid == "" or sid == []:
            self.cleanup_model()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        self.load_model(weight_root)

        if self.cpt is not None:
            self.setup_network()
            self.setup_vc_instance()

    def cleanup_model(self):
        """
        Cleans up the model and releases resources.
        """
        if self.hubert_model is not None:
            del self.net_g, self.n_spk, self.vc, self.hubert_model, self.tgt_sr
            self.hubert_model = self.net_g = self.n_spk = self.vc = self.tgt_sr = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        del self.net_g, self.cpt
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.cpt = None

    def load_model(self, weight_root):
        """
        Loads the model weights from the specified path.

        Args:
            weight_root (str): Path to the model weights.
        """
        self.cpt = (
            torch.load(weight_root, map_location="cpu")
            if os.path.isfile(weight_root)
            else None
        )

    def setup_network(self):
        """
        Sets up the network configuration based on the loaded checkpoint.
        """
        if self.cpt is not None:
            self.tgt_sr = self.cpt["config"][-1]
            self.cpt["config"][-3] = self.cpt["weight"]["emb_g.weight"].shape[0]
            self.use_f0 = self.cpt.get("f0", 1)

            self.version = self.cpt.get("version", "v1")
            self.text_enc_hidden_dim = 768 if self.version == "v2" else 256
            self.net_g = Synthesizer(
                *self.cpt["config"],
                use_f0=self.use_f0,
                text_enc_hidden_dim=self.text_enc_hidden_dim,
                is_half=self.config.is_half,
            )
            del self.net_g.enc_q
            self.net_g.load_state_dict(self.cpt["weight"], strict=False)
            self.net_g.eval().to(self.config.device)
            self.net_g = (
                self.net_g.half() if self.config.is_half else self.net_g.float()
            )

    def setup_vc_instance(self):
        """
        Sets up the voice conversion pipeline instance based on the target sampling rate and configuration.
        """
        if self.cpt is not None:
            self.vc = VC(self.tgt_sr, self.config)
            self.n_spk = self.cpt["config"][-3]
