import gradio as gr
import sys
import os
import logging

# Constants
DEFAULT_PORT = 6969
MAX_PORT_ATTEMPTS = 10

# Set up logging
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Add current directory to sys.path
now_dir = os.getcwd()
sys.path.append(now_dir)

# Import Tabs
from tabs.inference.inference import inference_tab
from tabs.train.train import train_tab
from tabs.extra.extra import extra_tab
from tabs.report.report import report_tab
from tabs.download.download import download_tab
from tabs.tts.tts import tts_tab
from tabs.voice_blender.voice_blender import voice_blender_tab
from tabs.plugins.plugins import plugins_tab
from tabs.settings.version import version_tab
from tabs.settings.lang import lang_tab
from tabs.settings.restart import restart_tab
from tabs.settings.presence import presence_tab, load_config_presence
from tabs.settings.flask_server import flask_server_tab
from tabs.settings.fake_gpu import fake_gpu_tab, gpu_available, load_fake_gpu
from tabs.settings.themes import theme_tab
from tabs.settings.precision import precision_tab

# Run prerequisites
from core import run_prerequisites_script

run_prerequisites_script(False, True, True, True)

# Initialize i18n
from assets.i18n.i18n import I18nAuto

i18n = I18nAuto()

# Start Discord presence if enabled
if load_config_presence():
    from assets.discord_presence import RPCManager

    RPCManager.start_presence()

# Check installation
import assets.installation_checker as installation_checker

installation_checker.check_installation()

# Start Flask server if enabled
from assets.flask.server import start_flask, load_config_flask

if load_config_flask():
    print("Starting Flask server")
    start_flask()

# Load theme
import assets.themes.loadThemes as loadThemes

my_applio = loadThemes.load_json() or "ParityError/Interstellar"

# Define Gradio interface
with gr.Blocks(theme=my_applio, title="Applio") as Applio:
    gr.Markdown("# Applio")
    gr.Markdown(
        i18n(
            "VITS-based Voice Conversion focused on simplicity, quality and performance."
        )
    )
    gr.Markdown(
        i18n(
            "[Support](https://discord.gg/IAHispano) — [Discord Bot](https://discord.com/oauth2/authorize?client_id=1144714449563955302&permissions=1376674695271&scope=bot%20applications.commands) — [Find Voices](https://applio.org/models) — [GitHub](https://github.com/IAHispano/Applio)"
        )
    )
    with gr.Tab(i18n("Inference")):
        inference_tab()

    with gr.Tab(i18n("Train")):
        if gpu_available() or load_fake_gpu():
            train_tab()
        else:
            gr.Markdown(
                i18n(
                    "Training is currently unsupported due to the absence of a GPU. To activate the training tab, navigate to the settings tab and enable the 'Fake GPU' option."
                )
            )

    with gr.Tab(i18n("TTS")):
        tts_tab()

    with gr.Tab(i18n("Voice Blender")):
        voice_blender_tab()

    with gr.Tab(i18n("Plugins")):
        plugins_tab()

    with gr.Tab(i18n("Download")):
        download_tab()

    with gr.Tab(i18n("Report a Bug")):
        report_tab()

    with gr.Tab(i18n("Extra")):
        extra_tab()

    with gr.Tab(i18n("Settings")):
        presence_tab()
        flask_server_tab()
        precision_tab()
        if not gpu_available():
            fake_gpu_tab()
        theme_tab()
        version_tab()
        lang_tab()
        restart_tab()


def launch_gradio():
    Applio.launch(share=True)

if __name__ == "__main__":
    launch_gradio()
