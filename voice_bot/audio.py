"""G.711 μ-law helpers.

Twilio Media Streams speak 8 kHz mono μ-law in 20 ms frames (160 bytes). We need
to decode/encode it ourselves to apply gain — Python's `audioop` was removed in
3.13, so the codec is implemented here. It's the standard BSD g711 algorithm.
"""

from __future__ import annotations

FRAME_BYTES = 160  # 20 ms at 8 kHz, 1 byte per sample
ULAW_SILENCE = 0xFF

_BIAS = 0x84
_CLIP = 8159  # in the 14-bit domain the encoder works in
_SEG_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _segment(value: int) -> int:
    for i, end in enumerate(_SEG_END):
        if value <= end:
            return i
    return 8


def _linear_to_ulaw(sample: int) -> int:
    """Encode a 16-bit signed sample (the reference Sun/BSD g711 algorithm)."""
    sample >>= 2  # 16-bit -> 14-bit, which is what the segment table indexes
    if sample < 0:
        sample = -sample
        mask = 0x7F
    else:
        mask = 0xFF
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS >> 2
    seg = _segment(sample)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((sample >> (seg + 1)) & 0x0F)) ^ mask


def _ulaw_to_linear(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


# Precomputed lookup tables — decoding runs on every frame, so avoid the math.
_DECODE = [_ulaw_to_linear(i) for i in range(256)]
_ENCODE = {}


def _encode(sample: int) -> int:
    cached = _ENCODE.get(sample)
    if cached is None:
        cached = _linear_to_ulaw(sample)
        _ENCODE[sample] = cached
    return cached


def apply_gain(ulaw: bytes, gain: float) -> bytes:
    """Scale μ-law audio amplitude. gain < 1.0 makes the caller quieter."""
    if gain == 1.0:
        return ulaw
    return bytes(_encode(int(_DECODE[b] * gain)) for b in ulaw)


def silence(milliseconds: int) -> bytes:
    """μ-law silence of the requested duration, rounded to whole 20 ms frames."""
    frames = max(0, round(milliseconds / 20))
    return bytes([ULAW_SILENCE]) * (frames * FRAME_BYTES)


def chunk_frames(ulaw: bytes) -> list[bytes]:
    """Split a μ-law buffer into 20 ms frames, zero-padding the tail."""
    frames = []
    for start in range(0, len(ulaw), FRAME_BYTES):
        frame = ulaw[start:start + FRAME_BYTES]
        if len(frame) < FRAME_BYTES:
            frame += bytes([ULAW_SILENCE]) * (FRAME_BYTES - len(frame))
        frames.append(frame)
    return frames
