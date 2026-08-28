import json
import struct
import wave

import pytest

from finchvox.audio_utils import combine_chunks


SAMPLE_RATE = 1000
CHANNELS = 2


def _write_segments(path, segments):
    samples = []
    for duration, amplitude in segments:
        frame_count = round(duration * SAMPLE_RATE)
        samples.extend([amplitude, amplitude] * frame_count)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(CHANNELS)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _duration_and_active_samples(path):
    with wave.open(str(path), "rb") as stream:
        duration = stream.getnframes() / stream.getframerate()
        samples = struct.unpack(
            f"<{stream.getnframes() * stream.getnchannels()}h",
            stream.readframes(stream.getnframes()),
        )
    return duration, [sample for sample in samples if sample]


def test_combine_chunks_compacts_only_excess_silence(tmp_path):
    audio_dir = tmp_path / "session" / "audio"
    audio_dir.mkdir(parents=True)
    chunk = audio_dir / "chunk_0000.wav"
    _write_segments(chunk, [(1.0, 1200), (3.0, 0), (1.0, -1200)])
    chunk.with_suffix(".json").write_text(
        json.dumps({"conversation_elapsed_seconds": 3.0})
    )

    output = tmp_path / "combined.wav"
    combine_chunks([(0, chunk)], output)

    duration, active_samples = _duration_and_active_samples(output)
    assert duration == pytest.approx(3.25, abs=0.011)
    assert active_samples == [1200] * 2000 + [-1200] * 2000


def test_combine_chunks_keeps_audio_with_matching_wall_duration(tmp_path):
    audio_dir = tmp_path / "session" / "audio"
    audio_dir.mkdir(parents=True)
    chunk = audio_dir / "chunk_0000.wav"
    _write_segments(chunk, [(1.0, 1200), (3.0, 0), (1.0, -1200)])
    chunk.with_suffix(".json").write_text(
        json.dumps({"conversation_elapsed_seconds": 4.8})
    )

    output = tmp_path / "combined.wav"
    combine_chunks([(0, chunk)], output)

    duration, _ = _duration_and_active_samples(output)
    assert duration == pytest.approx(5.0)


def test_combine_chunks_uses_trace_duration_as_safety_floor(tmp_path):
    session_dir = tmp_path / "session"
    audio_dir = session_dir / "audio"
    audio_dir.mkdir(parents=True)
    chunk = audio_dir / "chunk_0000.wav"
    _write_segments(chunk, [(1.0, 1200), (3.0, 0), (1.0, -1200)])
    chunk.with_suffix(".json").write_text(
        json.dumps({"conversation_elapsed_seconds": 2.0})
    )
    (session_dir / "trace_session.jsonl").write_text(
        json.dumps(
            {
                "start_time_unix_nano": 1_000_000_000,
                "end_time_unix_nano": 5_000_000_000,
            }
        )
    )

    output = tmp_path / "combined.wav"
    combine_chunks([(0, chunk)], output)

    duration, _ = _duration_and_active_samples(output)
    assert duration == pytest.approx(4.25, abs=0.011)


def test_combine_chunks_without_timing_metadata_is_lossless(tmp_path):
    audio_dir = tmp_path / "session" / "audio"
    audio_dir.mkdir(parents=True)
    chunk = audio_dir / "chunk_0000.wav"
    _write_segments(chunk, [(1.0, 1200), (3.0, 0), (1.0, -1200)])

    output = tmp_path / "combined.wav"
    combine_chunks([(0, chunk)], output)

    duration, active_samples = _duration_and_active_samples(output)
    assert duration == pytest.approx(5.0)
    assert active_samples == [1200] * 2000 + [-1200] * 2000
