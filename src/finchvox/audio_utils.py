"""Audio utilities for FinchVox trace viewer."""

import json
import math
import wave
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from loguru import logger


_SILENCE_RMS_THRESHOLD = 128
_SILENCE_WINDOW_SECONDS = 0.01
_MIN_PAUSE_SECONDS = 0.05
_DURATION_TOLERANCE_SECONDS = 0.25


def find_chunks(sessions_dir: Path, session_id: str) -> List[Tuple[int, Path]]:
    """
    Find all audio chunks for a given session_id.

    Args:
        sessions_dir: Base sessions directory (e.g., ~/.finchvox/sessions)
        session_id: Session ID to search for

    Returns:
        List of (chunk_number, chunk_path) tuples, sorted by chunk number
    """
    chunks = []

    audio_dir = sessions_dir / session_id / "audio"
    if audio_dir.exists():
        for chunk_file in audio_dir.glob("chunk_*.wav"):
            try:
                chunk_num = int(chunk_file.stem.split("_")[1])
                chunks.append((chunk_num, chunk_file))
            except (IndexError, ValueError) as e:
                logger.warning(f"Could not parse chunk number from {chunk_file}: {e}")

    chunks = list(set(chunks))
    chunks.sort(key=lambda x: x[0])
    return chunks


def combine_chunks(chunks: List[Tuple[int, Path]], output_file: Path) -> None:
    """
    Combine audio chunks into a single WAV file.

    Args:
        chunks: List of (chunk_number, chunk_path) tuples
        output_file: Path to write combined WAV file
    """
    if not chunks:
        logger.error("No chunks to combine")
        return

    # Get audio parameters from first chunk
    first_chunk = chunks[0][1]
    with wave.open(str(first_chunk), "rb") as wf:
        sample_rate = wf.getframerate()
        num_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()

    logger.info(
        f"Combining {len(chunks)} chunks: "
        f"{sample_rate}Hz, {num_channels} channels, {sample_width * 8}-bit"
    )

    pcm = bytearray()
    for chunk_num, chunk_path in chunks:
        logger.debug(f"Adding chunk {chunk_num}: {chunk_path.name}")

        with wave.open(str(chunk_path), "rb") as in_wf:
            if (
                in_wf.getframerate() != sample_rate
                or in_wf.getnchannels() != num_channels
                or in_wf.getsampwidth() != sample_width
            ):
                logger.warning(
                    f"Chunk {chunk_num} has different audio parameters, skipping"
                )
                continue
            pcm.extend(in_wf.readframes(in_wf.getnframes()))

    original_frames = len(pcm) // (num_channels * sample_width)
    expected_duration = _get_expected_recording_duration(chunks)
    if expected_duration is not None and sample_width == 2:
        target_frames = round(
            (expected_duration + _DURATION_TOLERANCE_SECONDS) * sample_rate
        )
        if original_frames > target_frames:
            pcm = _compact_silence(
                pcm,
                sample_rate=sample_rate,
                num_channels=num_channels,
                target_frames=target_frames,
            )

    total_frames = len(pcm) // (num_channels * sample_width)
    with wave.open(str(output_file), "wb") as out_wf:
        out_wf.setnchannels(num_channels)
        out_wf.setsampwidth(sample_width)
        out_wf.setframerate(sample_rate)
        out_wf.writeframes(pcm)

    duration_seconds = total_frames / sample_rate
    removed_seconds = (original_frames - total_frames) / sample_rate
    logger.info(
        f"Combined {len(chunks)} chunks into {output_file.name} "
        f"({duration_seconds:.1f} seconds, removed {removed_seconds:.1f} seconds "
        "of excess silence)"
    )


def _get_expected_recording_duration(
    chunks: List[Tuple[int, Path]],
) -> float | None:
    """Return the best wall-clock duration recorded alongside the chunks."""
    metadata_duration = _get_metadata_duration(chunks)
    trace_duration = _get_trace_duration(chunks[0][1].parent.parent)
    durations = [
        duration
        for duration in (metadata_duration, trace_duration)
        if duration is not None and duration > 0
    ]
    return max(durations) if durations else None


def _get_metadata_duration(chunks: List[Tuple[int, Path]]) -> float | None:
    metadata = []
    for _, chunk_path in chunks:
        try:
            metadata.append(json.loads(chunk_path.with_suffix(".json").read_text()))
        except (OSError, json.JSONDecodeError):
            continue

    if not metadata:
        return None

    precise_elapsed = metadata[-1].get("conversation_elapsed_seconds")
    if isinstance(precise_elapsed, (int, float)) and precise_elapsed > 0:
        return float(precise_elapsed)

    conversation_start = metadata[0].get("conversation_start")
    captured_at = metadata[-1].get("timestamp")
    if not isinstance(conversation_start, str) or not isinstance(captured_at, str):
        return None

    try:
        started = datetime.fromisoformat(conversation_start)
        try:
            ended = datetime.fromisoformat(captured_at)
        except ValueError:
            ended = datetime.strptime(captured_at, "%Y%m%d_%H%M%S")
        if started.tzinfo is not None and ended.tzinfo is None:
            ended = ended.replace(tzinfo=started.tzinfo)
        return (ended - started).total_seconds()
    except ValueError:
        return None


def _get_trace_duration(session_dir: Path) -> float | None:
    starts = []
    ends = []
    for trace_file in session_dir.glob("trace_*.jsonl"):
        try:
            lines = trace_file.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                span = json.loads(line)
                starts.append(int(span["start_time_unix_nano"]))
                ends.append(int(span["end_time_unix_nano"]))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
    if not starts or not ends:
        return None
    return (max(ends) - min(starts)) / 1_000_000_000


def _compact_silence(
    pcm: bytes | bytearray,
    *,
    sample_rate: int,
    num_channels: int,
    target_frames: int,
) -> bytearray:
    """Shorten silent runs while leaving all non-silent samples untouched."""
    frame_width = num_channels * 2
    total_frames = len(pcm) // frame_width
    frames_to_remove = total_frames - target_frames
    if frames_to_remove <= 0:
        return bytearray(pcm)

    silent_runs = _find_silent_runs(pcm, sample_rate, num_channels, total_frames)
    minimum_pause_frames = round(sample_rate * _MIN_PAUSE_SECONDS)
    capacities = [
        max(0, end - start - minimum_pause_frames) for start, end in silent_runs
    ]
    total_capacity = sum(capacities)
    if total_capacity == 0:
        logger.warning(
            "Audio is longer than the recorded session but contains no removable silence"
        )
        return bytearray(pcm)

    removal_goal = min(frames_to_remove, total_capacity)
    removals = _allocate_removals(capacities, removal_goal, total_capacity)

    compacted = _remove_silence(pcm, frame_width, total_frames, silent_runs, removals)
    if removal_goal < frames_to_remove:
        logger.warning(
            f"Could only remove {removal_goal / sample_rate:.1f} of "
            f"{frames_to_remove / sample_rate:.1f} excess audio seconds without "
            "cutting speech"
        )
    return compacted


def _find_silent_runs(
    pcm: bytes | bytearray,
    sample_rate: int,
    num_channels: int,
    total_frames: int,
) -> list[tuple[int, int]]:
    window_frames = max(1, round(sample_rate * _SILENCE_WINDOW_SECONDS))
    samples = memoryview(pcm).cast("h")
    silent_runs: list[tuple[int, int]] = []
    for start in range(0, total_frames, window_frames):
        end = min(total_frames, start + window_frames)
        window = samples[start * num_channels : end * num_channels]
        rms = math.sqrt(sum(sample * sample for sample in window) / len(window))
        if rms > _SILENCE_RMS_THRESHOLD:
            continue
        if silent_runs and silent_runs[-1][1] == start:
            silent_runs[-1] = (silent_runs[-1][0], end)
        else:
            silent_runs.append((start, end))
    return silent_runs


def _allocate_removals(
    capacities: list[int], removal_goal: int, total_capacity: int
) -> list[int]:
    removals = [capacity * removal_goal // total_capacity for capacity in capacities]
    remainder = removal_goal - sum(removals)
    for index in sorted(
        range(len(capacities)), key=capacities.__getitem__, reverse=True
    ):
        if remainder == 0:
            break
        available = capacities[index] - removals[index]
        extra = min(available, remainder)
        removals[index] += extra
        remainder -= extra
    return removals


def _remove_silence(
    pcm: bytes | bytearray,
    frame_width: int,
    total_frames: int,
    silent_runs: list[tuple[int, int]],
    removals: list[int],
) -> bytearray:
    cuts = []
    for (start, end), remove in zip(silent_runs, removals):
        if remove <= 0:
            continue
        cut_start = start + (end - start - remove) // 2
        cuts.append((cut_start, cut_start + remove))

    compacted = bytearray()
    cursor = 0
    for start, end in cuts:
        compacted.extend(pcm[cursor * frame_width : start * frame_width])
        cursor = end
    compacted.extend(pcm[cursor * frame_width : total_frames * frame_width])
    return compacted
