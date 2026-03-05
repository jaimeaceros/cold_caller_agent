"""
CLI voice test — pyaudio direct ↔ RealtimeSession.

Bypasses the browser/WebSocket/WebAudio stack entirely.
Uses the same audio format as the Azure VoiceLive SDK playground:
  24 kHz, mono, PCM16, 1200-frame chunks (50 ms).

Usage:
    pip install pyaudio
    python test_voice.py lead_001        # Ctrl+C to hang up
    python test_voice.py lead_001 500    # custom VAD silence (ms)
"""

import asyncio
import base64
import logging
import os
import queue
import signal
import sys
import uuid

import pyaudio
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# AUDIO CONFIG — VoiceLive SDK defaults
# ============================================================

RATE = 24_000          # 24 kHz
CHANNELS = 1           # mono
FORMAT = pyaudio.paInt16
FRAME_SIZE = 1200      # 50 ms at 24 kHz
BYTES_PER_FRAME = FRAME_SIZE * 2  # 16-bit = 2 bytes/sample


# ============================================================
# MAIN
# ============================================================

async def run_voice_call(lead_id: str, vad_silence_ms: int = 500):
    from agent.realtime import RealtimeSession

    session = RealtimeSession(mode="audio")
    session_id = f"pyaudio_{uuid.uuid4().hex[:8]}"

    # -- asyncio / threading plumbing --
    loop = asyncio.get_running_loop()
    mic_queue: asyncio.Queue[bytes] = asyncio.Queue()
    playback_queue: queue.Queue[bytes] = queue.Queue()
    shutdown_event = asyncio.Event()

    # ---- callbacks ----

    async def on_audio_delta(b64_chunk: str):
        """Decode base64 PCM16 from API → playback queue."""
        pcm = base64.b64decode(b64_chunk)
        playback_queue.put(pcm)

    async def on_audio_done():
        pass

    async def on_transcript_done(transcript: str):
        print(f"\n[Agent] {transcript}")

    async def on_input_transcript(transcript: str):
        print(f"\n[You  ] {transcript}")

    async def on_speech_started():
        """User started talking — clear buffered agent audio (interruption)."""
        cleared = 0
        while not playback_queue.empty():
            try:
                playback_queue.get_nowait()
                cleared += 1
            except queue.Empty:
                break
        if cleared:
            logger.debug(f"Interruption: cleared {cleared} playback chunks")

    async def on_speech_stopped():
        pass

    async def on_call_ended(outcome):
        print(f"\n--- Call ended ---")
        if outcome:
            print(f"  Outcome: {outcome.get('outcome', '?')}")
            print(f"  Reason:  {outcome.get('reason', '?')}")
            print(f"  Summary: {outcome.get('summary', '?')}")
        shutdown_event.set()

    async def on_error(message: str):
        print(f"\n[ERROR] {message}")

    session.on_audio_delta = on_audio_delta
    session.on_audio_done = on_audio_done
    session.on_transcript_done = on_transcript_done
    session.on_input_transcript = on_input_transcript
    session.on_speech_started = on_speech_started
    session.on_speech_stopped = on_speech_stopped
    session.on_call_ended = on_call_ended
    session.on_error = on_error

    # ---- pyaudio callbacks (run on OS audio threads) ----

    def mic_callback(in_data, frame_count, time_info, status):
        """Called by pyaudio mic stream — push raw bytes to async queue."""
        loop.call_soon_threadsafe(mic_queue.put_nowait, in_data)
        return (None, pyaudio.paContinue)

    def speaker_callback(in_data, frame_count, time_info, status):
        """Called by pyaudio speaker stream — drain playback queue."""
        needed = frame_count * 2  # 16-bit mono
        buf = b""
        while len(buf) < needed:
            try:
                chunk = playback_queue.get_nowait()
                buf += chunk
            except queue.Empty:
                break
        # Pad with silence if underrun
        if len(buf) < needed:
            buf += b"\x00" * (needed - len(buf))
        return (buf[:needed], pyaudio.paContinue)

    # ---- mic sender coroutine ----

    async def mic_sender():
        """Drain mic_queue → send_audio in a loop."""
        while not shutdown_event.is_set():
            try:
                data = await asyncio.wait_for(mic_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            b64 = base64.b64encode(data).decode("ascii")
            try:
                await session.send_audio(b64)
            except Exception as e:
                logger.error(f"send_audio error: {e}")
                break

    # ---- Ctrl+C handler (cross-platform) ----

    def handle_sigint(sig, frame):
        print("\n\nCtrl+C — hanging up...")
        loop.call_soon_threadsafe(shutdown_event.set)

    signal.signal(signal.SIGINT, handle_sigint)

    # ---- run ----

    pa = pyaudio.PyAudio()
    mic_stream = None
    speaker_stream = None

    try:
        # 1. Connect + configure
        await session.connect()
        call_session = session.init_call_session(session_id, lead_id)
        await session.configure(vad_silence_ms=vad_silence_ms)

        prospect_name = call_session.lead.get("contact", {}).get("name", "Unknown")
        print(f"\n{'='*50}")
        print(f"  Voice call: {prospect_name} ({lead_id})")
        print(f"  VAD silence: {vad_silence_ms}ms")
        print(f"  Ctrl+C to hang up")
        print(f"{'='*50}\n")

        # 2. Open pyaudio streams
        mic_stream = pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=FRAME_SIZE,
            stream_callback=mic_callback,
        )

        speaker_stream = pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            output=True,
            frames_per_buffer=FRAME_SIZE,
            stream_callback=speaker_callback,
        )

        mic_stream.start_stream()
        speaker_stream.start_stream()

        # 3. Trigger greeting
        await session.send_text()

        # 4. Run event loop + mic sender concurrently
        event_task = asyncio.create_task(session.run_event_loop())
        sender_task = asyncio.create_task(mic_sender())

        await shutdown_event.wait()

        # 5. Cleanup tasks
        event_task.cancel()
        sender_task.cancel()
        for t in [event_task, sender_task]:
            try:
                await t
            except asyncio.CancelledError:
                pass

    finally:
        # Stop pyaudio
        if mic_stream:
            mic_stream.stop_stream()
            mic_stream.close()
        if speaker_stream:
            speaker_stream.stop_stream()
            speaker_stream.close()
        pa.terminate()

        # Print summary
        summary = session.get_call_summary()
        if summary and summary["total_turns"] > 0:
            print(f"\n{'='*50}")
            print(f"  Call summary")
            print(f"  Turns: {summary['total_turns']}")
            print(f"  Duration: {summary['duration_seconds']:.0f}s")
            print(f"  Phase: {summary['final_phase']}")
            print(f"  Outcome: {summary['call_outcome'] or 'N/A'}")
            qd = summary["qualifying_data"]
            qualified = [f"{k}={v}" for k, v in qd.items() if v]
            if qualified:
                print(f"  BANT: {', '.join(qualified)}")
            print(f"{'='*50}")

        await session.close()


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_voice.py <lead_id> [vad_silence_ms]")
        print("  e.g. python test_voice.py lead_001")
        print("  e.g. python test_voice.py lead_001 500")
        sys.exit(1)

    lead_id = sys.argv[1]
    vad_silence_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 500

    asyncio.run(run_voice_call(lead_id, vad_silence_ms))


if __name__ == "__main__":
    main()
