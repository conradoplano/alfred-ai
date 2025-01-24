import asyncio
import logging
import base64
import os, sys, json
from typing import Any, Dict
from pathlib import Path
from enum import Enum, auto

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.abspath(os.path.join(current_dir, '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from utils.audio_playback import AudioPlayer
from utils.audio_capture import AudioCapture, AudioCaptureEventHandler
from utils.function_tool import FunctionTool
from realtime_ai.aio.realtime_ai_client import RealtimeAIClient
from realtime_ai.models.realtime_ai_options import RealtimeAIOptions
from realtime_ai.models.audio_stream_options import AudioStreamOptions
from realtime_ai.aio.realtime_ai_event_handler import RealtimeAIEventHandler
from realtime_ai.models.realtime_ai_events import *
from user_functions import user_functions

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # Streaming logs to the console
)

# Specific loggers for mentioned packages
logging.getLogger("utils.audio_playback").setLevel(logging.ERROR)
logging.getLogger("utils.audio_capture").setLevel(logging.ERROR)
logging.getLogger("utils.vad").setLevel(logging.ERROR)
logging.getLogger("realtime_ai").setLevel(logging.ERROR)

# Root logger for general logging
logger = logging.getLogger()

SCRIPT_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = SCRIPT_DIR / "../resources"


class ConversationState(Enum):
    IDLE = auto()
    KEYWORD_DETECTED = auto()
    CONVERSATION_ACTIVE = auto()


class MyAudioCaptureEventHandler(AudioCaptureEventHandler):
    def __init__(self, client: RealtimeAIClient, event_handler: "MyRealtimeEventHandler", event_loop):
        """
        Initializes the event handler.
        
        :param client: Instance of RealtimeClient.
        :param event_handler: Instance of MyRealtimeEventHandler
        :param event_loop: The asyncio event loop.
        """
        self._client = client
        self._event_handler = event_handler
        self._state = ConversationState.IDLE
        self._silence_timeout = 10  # Silence timeout in seconds for rearming keyword detection
        self._silence_timer_task = None
        self._event_loop = event_loop

    def send_audio_data(self, audio_data: bytes):
        """
        Sends audio data to the RealtimeClient.

        :param audio_data: Raw audio data in bytes.
        """
        if self._state == ConversationState.CONVERSATION_ACTIVE:
            logger.info("Sending audio data to the client.")
            asyncio.run_coroutine_threadsafe(self._client.send_audio(audio_data), self._event_loop)

    def on_speech_start(self):
        """
        Handles actions to perform when speech starts.
        """
        logger.info("Local VAD: User speech started")
        logger.info(f"on_speech_start: Current state: {self._state}")

        if self._state == ConversationState.KEYWORD_DETECTED or self._state == ConversationState.CONVERSATION_ACTIVE:
            asyncio.run_coroutine_threadsafe(self._set_state(ConversationState.CONVERSATION_ACTIVE), self._event_loop)
            asyncio.run_coroutine_threadsafe(self._cancel_silence_timer(), self._event_loop)

        if (self._client.options.turn_detection is None and
            self._event_handler.is_audio_playing() and
            self._state == ConversationState.CONVERSATION_ACTIVE):
            logger.info("User started speaking while assistant is responding; interrupting the assistant's response.")
            asyncio.run_coroutine_threadsafe(self._client.clear_input_audio_buffer(), self._event_loop)
            asyncio.run_coroutine_threadsafe(self._client.cancel_response(), self._event_loop)
            self._event_handler.audio_player.drain_and_restart()

    def on_speech_end(self):
        """
        Handles actions to perform when speech ends.
        """
        logger.info("Local VAD: User speech ended")
        logger.info(f"on_speech_end: Current state: {self._state}")

        if self._state == ConversationState.CONVERSATION_ACTIVE and self._client.options.turn_detection is None:
            logger.debug("Using local VAD; requesting the client to generate a response after speech ends.")
            asyncio.run_coroutine_threadsafe(self._client.generate_response(), self._event_loop)
            logger.debug("Conversation is active. Starting silence timer.")
            asyncio.run_coroutine_threadsafe(self._start_silence_timer(), self._event_loop)

    def on_keyword_detected(self, result):
        """
        Called when a keyword is detected.

        :param result: The recognition result containing details about the detected keyword.
        """
        logger.info(f"Local Keyword: User keyword detected: {result}")
        asyncio.run_coroutine_threadsafe(self._client.send_text("Hello"), self._event_loop)
        asyncio.run_coroutine_threadsafe(self._set_state(ConversationState.KEYWORD_DETECTED), self._event_loop)
        asyncio.run_coroutine_threadsafe(self._start_silence_timer(), self._event_loop)

    async def _silence_timer_coroutine(self):
        await asyncio.sleep(self._silence_timeout)
        await self._reset_state_due_to_silence()

    async def _start_silence_timer(self):
        await self._cancel_silence_timer()
        self._silence_timer_task = asyncio.create_task(self._silence_timer_coroutine(), name="SilenceTimer")

    async def _cancel_silence_timer(self):
        if self._silence_timer_task and not self._silence_timer_task.done():
            self._silence_timer_task.cancel()
            try:
                await self._silence_timer_task
            except asyncio.CancelledError:
                logger.debug("Silence timer cancelled.")
            self._silence_timer_task = None

    async def _reset_state_due_to_silence(self):
        if self._event_handler.is_audio_playing() or self._event_handler.is_function_processing():
            logger.info("Assistant is responding or processing a function. Waiting to reset keyword detection.")
            await self._start_silence_timer()
            return

        logger.info("Silence timeout reached. Rearming keyword detection.")
        logger.debug("Clearing input audio buffer.")
        asyncio.run_coroutine_threadsafe(self._client.clear_input_audio_buffer(), self._event_loop)
        asyncio.run_coroutine_threadsafe(self._set_state(ConversationState.IDLE), self._event_loop)

    async def _set_state(self, new_state: ConversationState):
        logger.debug(f"Transitioning from {self._state} to {new_state}")
        self._state = new_state
        if new_state != ConversationState.CONVERSATION_ACTIVE:
            await self._cancel_silence_timer()


class MyRealtimeEventHandler(RealtimeAIEventHandler):
    def __init__(self, audio_player: AudioPlayer, functions: FunctionTool):
        super().__init__()
        self._audio_player = audio_player
        self._lock = asyncio.Lock()
        self._client = None
        self._current_item_id = None
        self._current_audio_content_index = None
        self._call_id_to_function_name = {}
        self._functions = functions
        self._function_processing = False

    @property
    def audio_player(self):
        return self._audio_player

    def get_current_conversation_item_id(self):
        return self._current_item_id
    
    def get_current_audio_content_id(self):
        return self._current_audio_content_index
    
    def is_audio_playing(self):
        return self._audio_player.is_audio_playing()
    
    def is_function_processing(self):
        return self._function_processing
    
    def set_client(self, client: RealtimeAIClient):
        self._client = client

    async def on_error(self, event: ErrorEvent) -> None:
        logger.error(f"Error occurred: {event.error.message}")

    async def on_input_audio_buffer_speech_stopped(self, event: InputAudioBufferSpeechStopped) -> None:
        logger.info(f"Server VAD: Speech stopped at {event.audio_end_ms}ms, Item ID: {event.item_id}")

    async def on_input_audio_buffer_cleared(self, event: InputAudioBufferCleared):
        logger.info("Input audio buffer cleared.")

    async def on_reconnected(self, event: ReconnectedEvent) -> None:
        logger.info("Reconnected...")

    async def on_input_audio_buffer_committed(self, event: InputAudioBufferCommitted) -> None:
        logger.debug(f"Audio Buffer Committed: {event.item_id}")

    async def on_conversation_item_created(self, event: ConversationItemCreated) -> None:
        logger.debug(f"New Conversation Item: {event.item}")

    async def on_response_created(self, event: ResponseCreated) -> None:
        logger.debug(f"Response Created: {event.response}")

    async def on_response_content_part_added(self, event: ResponseContentPartAdded) -> None:
        logger.debug(f"New Part Added: {event.part}")

    async def on_response_audio_delta(self, event: ResponseAudioDelta) -> None:
        logger.debug(f"Received audio delta for Response ID {event.response_id}, Item ID {event.item_id}, Content Index {event.content_index}")
        self._current_item_id = event.item_id
        self._current_audio_content_index = event.content_index
        self.handle_audio_delta(event)

    async def on_response_audio_transcript_delta(self, event: ResponseAudioTranscriptDelta) -> None:
        logger.info(f"Assistant transcription delta: {event.delta}")

    async def on_rate_limits_updated(self, event: RateLimitsUpdated) -> None:
        for rate in event.rate_limits:
            logger.debug(f"Rate Limit: {rate.name}, Remaining: {rate.remaining}")

    async def on_conversation_item_input_audio_transcription_completed(self, event: ConversationItemInputAudioTranscriptionCompleted) -> None:
        logger.info(f"User transcription complete: {event.transcript}")

    async def on_response_audio_done(self, event: ResponseAudioDone) -> None:
        logger.debug(f"Audio done for response ID {event.response_id}, item ID {event.item_id}")

    async def on_response_audio_transcript_done(self, event: ResponseAudioTranscriptDone) -> None:
        logger.debug(f"Audio transcript done: '{event.transcript}' for response ID {event.response_id}")

    async def on_response_content_part_done(self, event: ResponseContentPartDone) -> None:
        part_type = event.part.get("type")
        part_text = event.part.get("text", "")
        logger.debug(f"Content part done: '{part_text}' of type '{part_type}' for response ID {event.response_id}")

    async def on_response_output_item_done(self, event: ResponseOutputItemDone) -> None:
        item_content = event.item.get("content", [])
        if item_content:
            for item in item_content:
                if item.get("type") == "audio":
                    transcript = item.get("transcript")
                    if transcript:
                        logger.info(f"Assistant transcription complete: {transcript}")

    async def on_response_done(self, event: ResponseDone) -> None:
        logger.debug(f"Assistant's response completed with status '{event.response.get('status')}' and ID '{event.response.get('id')}'")

    async def on_session_created(self, event: SessionCreated) -> None:
        logger.info(f"Session created: {event.session}")

    async def on_session_updated(self, event: SessionUpdated) -> None:
        logger.info(f"Session updated: {event.session}")

    async def on_input_audio_buffer_speech_started(self, event: InputAudioBufferSpeechStarted) -> None:
        logger.info(f"Server VAD: User speech started at {event.audio_start_ms}ms for item ID {event.item_id}")
        if self._client.options.turn_detection is not None:
            await self._client.clear_input_audio_buffer()
            await self._client.cancel_response()
            await asyncio.threads.to_thread(self._audio_player.drain_and_restart)

    async def on_response_output_item_added(self, event: ResponseOutputItemAdded) -> None:
        logger.debug(f"Output item added for response ID {event.response_id} with item: {event.item}")
        if event.item.get("type") == "function_call":
            call_id = event.item.get("call_id")
            function_name = event.item.get("name")
            if call_id and function_name:
                # Properly acquire the lock with 'await' and spread the usage over two lines
                await self._lock.acquire()  # Wait until the lock is available, then acquire it
                try:
                    self._call_id_to_function_name[call_id] = function_name
                    logger.debug(f"Registered function call. Call ID: {call_id}, Function Name: {function_name}")
                finally:
                    # Ensure the lock is released even if an exception occurs
                    self._lock.release()
            else:
                logger.warning("Function call item missing 'call_id' or 'name' fields.")

    async def on_response_function_call_arguments_delta(self, event: ResponseFunctionCallArgumentsDelta) -> None:
        logger.debug(f"Function call arguments delta for call ID {event.call_id}: {event.delta}")

    async def on_response_function_call_arguments_done(self, event: ResponseFunctionCallArgumentsDone) -> None:
        call_id = event.call_id
        arguments_str = event.arguments

        # Acquire lock using asynchronous method
        await self._lock.acquire()
        try:
            function_name = self._call_id_to_function_name.pop(call_id, None)
        finally:
            # Make sure the lock is released even if an exception is raised
            self._lock.release()

        if not function_name:
            logger.error(f"No function name found for call ID: {call_id}")
            return

        try:
            self._function_processing = True
            logger.info(f"Executing function '{function_name}' with arguments: {arguments_str} for call ID {call_id}")
            function_output = await asyncio.threads.to_thread(self._functions.execute, function_name, arguments_str)
            logger.info(f"Function output for call ID {call_id}: {function_output}")
            await self._client.generate_response_from_function_call(call_id, function_output)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse arguments for call ID {call_id}: {e}")
            return
        finally:
            self._function_processing = False

    def on_unhandled_event(self, event_type: str, event_data: Dict[str, Any]):
        logger.warning(f"Unhandled Event: {event_type} - {event_data}")

    def handle_audio_delta(self, event: ResponseAudioDelta):
        delta_audio = event.delta
        if delta_audio:
            try:
                audio_bytes = base64.b64decode(delta_audio)
                self._audio_player.enqueue_audio_data(audio_bytes)
            except base64.binascii.Error as e:
                logger.error(f"Failed to decode audio delta: {e}")
        else:
            logger.warning("Received 'ResponseAudioDelta' event without 'delta' field.")


def get_vad_configuration(use_server_vad=False):
    """
    Configures the VAD settings based on the specified preference.

    :param use_server_vad: Boolean indicating whether to use server-side VAD.
                           Default is False for local VAD.
    :return: Dictionary representing the VAD configuration suitable for RealtimeAIOptions.
    """
    if use_server_vad:
        return {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 200
        }
    else:
        return None  # Local VAD typically requires no special configuration


def get_openai_configuration():
    # The Azure endpoint shall be in the format: "wss://<service-name>.openai.azure.com/openai/realtime"
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = None
    azure_api_version = None

    if not azure_endpoint:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.error("OpenAI API key not found. Please set the OPENAI_API_KEY environment variable.")
            return None, None, None
    else:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        azure_api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-01-preview")

        if not api_key or not azure_endpoint or not azure_api_version:
            logger.error("Please set the AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, and AZURE_OPENAI_API_VERSION environment variables.")
            return None, None, None

    return azure_endpoint, api_key, azure_api_version


async def main():
    """
    Main function to initialize and run the audio processing and realtime client asynchronously.
    """
    client = None
    audio_player = None
    audio_capture = None

    try:

        azure_openai_endpoint, api_key, azure_api_version = get_openai_configuration()
        if not api_key:
            return

        functions = FunctionTool(functions=user_functions)

        # Define RealtimeOptions
        options = RealtimeAIOptions(
            api_key=api_key,
            model="gpt-4o-realtime-preview",
            modalities=["audio", "text"],
            instructions="You are a helpful assistant. Respond concisely and at most one sentence. You have access to a variety of tools to analyze, translate and review text and code.",
            turn_detection=get_vad_configuration(use_server_vad=False),
            tools=functions.definitions,
            tool_choice="auto",
            temperature=0.8,
            voice="ballad",
            enable_auto_reconnect=True,
            azure_openai_endpoint=azure_openai_endpoint,
            azure_openai_api_version=azure_api_version
        )

        # Define AudioStreamOptions
        stream_options = AudioStreamOptions(
            sample_rate=24000,
            channels=1,
            bytes_per_sample=2
        )

        # Initialize AudioPlayer
        audio_player = AudioPlayer(enable_wave_capture=False)

        # Initialize RealtimeAIClient with MyRealtimeEventHandler to handle events
        event_handler = MyRealtimeEventHandler(audio_player=audio_player, functions=functions)
        client = RealtimeAIClient(options, stream_options, event_handler)
        event_handler.set_client(client)
        await client.start()

        loop = asyncio.get_running_loop()
        
        audio_capture_event_handler = MyAudioCaptureEventHandler(
            client=client,
            event_handler=event_handler,
            event_loop=loop,
        )
        vad_parameters={
                "sample_rate": 24000,
                "chunk_size": 1024,
                "window_duration": 1.5,
                "silence_ratio": 1.5,
                "min_speech_duration": 0.3,
                "min_silence_duration": 1.0
            }
        if USE_SILERO_VAD_MODEL:
            logger.info("using Silero VAD...")
            vad_parameters["model_path"] = str(RESOURCES_DIR / "silero_vad.onnx")
        else:
            logger.info("using VoiceActivityDetector...")

        # Initialize AudioCapture with the event handler
        audio_capture = AudioCapture(
            event_handler=audio_capture_event_handler,
            sample_rate=24000,
            channels=1,
            frames_per_buffer=1024,
            buffer_duration_sec=1.0,
            cross_fade_duration_ms=20,
            vad_parameters=vad_parameters,
            enable_wave_capture=False,
            keyword_model_file=str(RESOURCES_DIR / "kws.table"),
        )

        logger.info("Recording... Press Ctrl+C to stop.")
        audio_player.start()
        audio_capture.start()

        # Keep the loop running while the stream is active
        while True:
            await asyncio.sleep(1)  # Sleep to allow other tasks to run

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown initiated by user.")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")

    finally:
        # Ensure resources are cleaned up properly
        if audio_player:
            audio_player.stop()
            audio_player.close()
            logger.info("Audio player stopped and closed.")

        if audio_capture:
            audio_capture.stop()
            audio_capture.close()
            logger.info("Audio capture stopped and closed.")

        if client:
            try:
                logger.info("Stopping client...")
                await client.stop()
                logger.info("Client stopped gracefully.")
            except Exception as e:
                logger.error(f"Error during client shutdown: {e}")

        logger.info("Shutdown complete.")


if __name__ == "__main__":
    USE_SILERO_VAD_MODEL = True
    asyncio.run(main())
