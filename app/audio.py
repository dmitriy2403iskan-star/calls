"""Перекодирование звука между телефонией и Gemini.

Twilio: μ-law, 8 кГц, моно (base64).
Gemini Live: вход PCM16 16 кГц, выход PCM16 24 кГц.
Без audioop (его убрали в Python 3.13) — только numpy."""
import base64

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        table[i] = -sample if sign else sample
    return table


_DECODE = _build_decode_table()


def ulaw_decode(data: bytes) -> np.ndarray:
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def ulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), _CLIP) + _BIAS
    exponent = np.floor(np.log2(x)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (x >> (exponent + 3)) & 0x0F
    u = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return u.astype(np.uint8).tobytes()


def twilio_to_gemini(payload_b64: str) -> bytes:
    """μ-law 8k (base64) -> PCM16 16k (bytes)."""
    pcm8 = ulaw_decode(base64.b64decode(payload_b64)).astype(np.float32)
    n = len(pcm8)
    if n == 0:
        return b""
    x_new = np.arange(2 * n) / 2.0
    pcm16 = np.interp(x_new, np.arange(n), pcm8)
    return pcm16.astype(np.int16).tobytes()


class Downsampler:
    """PCM16 24k -> μ-law 8k (base64). Хранит хвост между кусками."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._rest = np.zeros(0, dtype=np.int16)
        self._odd = b""

    def convert(self, pcm24: bytes) -> str:
        pcm24 = self._odd + pcm24
        cut = len(pcm24) - len(pcm24) % 2
        self._odd = pcm24[cut:]
        samples = np.concatenate([self._rest, np.frombuffer(pcm24[:cut], dtype=np.int16)])
        usable = len(samples) - len(samples) % 3
        self._rest = samples[usable:]
        if usable == 0:
            return ""
        # усредняем по 3 отсчёта — простой ФНЧ + прореживание 24k -> 8k
        pcm8 = samples[:usable].astype(np.int32).reshape(-1, 3).mean(axis=1)
        return base64.b64encode(ulaw_encode(pcm8)).decode()
