"""dna_engine.py — ถอด DNA code เป็น gate array 0/1 (invariant #2)

รูปแบบที่รองรับ (fail closed -> DNAError ถ้า decode ไม่ได้):
  - "bypass:N"     -> [1]*N                              (เปิดทุก slot)
  - "[1, N]"       -> [1]*N                              (bypass แบบ array เดิม)
  - length-encoded -> "[len][value][len][value]..."      (champion จาก Hybrid Multi-Mutation)
        numbers = [length, mutation_rate, dna_seed, *mutation_seeds]
        base = PCG64(dna_seed).integers(0,2,length) ; base[0]=1
        ทุก mutation_seed: flip bit ที่ PCG64(seed).random(length) < rate ; base[0]=1

invariant:
  - ผลลัพธ์เป็น list[int] ค่า {0,1} ความยาว = length
  - dna[0] == 1 เสมอ (แถวแรกต้องเปิด)
  - decode ให้ผลเดิมทุกครั้ง (deterministic) และ "ตรงกับ array ที่ตัว encode สร้าง"
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np


class DNAError(ValueError):
    """DNA spec ผิดรูปแบบ หรือ decode ไม่ได้ -> fail closed"""


@dataclass(frozen=True)
class DNASpec:
    kind: str                                   # 'bypass' | 'stream'
    length: int
    dna_seed: int | None = None
    mutation_rate: float = 0.0
    mutation_seeds: tuple[int, ...] = field(default_factory=tuple)


# ---- length-encoded number stream: "[len][value]..." ----------------------
def decode_number_stream(encoded: str) -> list[int]:
    if not encoded or not encoded.isdigit():
        raise DNAError("DNA string ต้องเป็นสตริงตัวเลขที่ไม่ว่าง")
    values: list[int] = []
    i = 0
    while i < len(encoded):
        width = int(encoded[i])
        i += 1
        if width <= 0:
            raise DNAError("token width ต้อง > 0")
        nxt = i + width
        if nxt > len(encoded):
            raise DNAError("DNA string จบก่อนอ่าน token ครบ")
        values.append(int(encoded[i:nxt]))
        i = nxt
    return values


def normalize_mutation_rate(raw_rate: int | float) -> float:
    """ตีความ 10 = 10%; ค่าที่ <= 1 ถือเป็นความน่าจะเป็นตรง ๆ"""
    rate = float(raw_rate)
    if rate < 0:
        raise DNAError("mutation rate ติดลบไม่ได้")
    if rate > 1:
        rate /= 100.0
    if rate > 1:
        raise DNAError("mutation rate เกิน 100% ไม่ได้")
    return rate


# ---- parse: คืน DNASpec (kind + พารามิเตอร์) ------------------------------
def parse_dna_spec(dna_code: str) -> DNASpec:
    if not isinstance(dna_code, str) or not dna_code.strip():
        raise DNAError(f"dna_code ผิดรูปแบบ: {dna_code!r}")
    text = dna_code.strip()

    # bypass:N
    if text.lower().startswith("bypass:"):
        raw = text.split(":", 1)[1].strip()
        try:
            length = int(raw)
        except ValueError as exc:
            raise DNAError("bypass length ต้องเป็นจำนวนเต็ม") from exc
        if length <= 0:
            raise DNAError("bypass length ต้อง > 0")
        return DNASpec("bypass", length)

    # [1, N]  (bypass array เดิม)
    if text.startswith("["):
        try:
            arr = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DNAError("bypass array ต้องเป็น [1, length]") from exc
        if (not isinstance(arr, list) or len(arr) != 2
                or type(arr[0]) is not int or type(arr[1]) is not int):
            raise DNAError("bypass array ต้องเป็น [1, length]")
        if arr[0] != 1:
            raise DNAError("bypass array รองรับค่า 1 เท่านั้น")
        if arr[1] <= 0:
            raise DNAError("bypass length ต้อง > 0")
        return DNASpec("bypass", arr[1])

    # length-encoded stream: [length, rate, dna_seed, *mutation_seeds]
    numbers = decode_number_stream(text)
    if len(numbers) < 3:
        raise DNAError("stream ต้องเข้ารหัสอย่างน้อย length, rate, dna_seed")
    length = int(numbers[0])
    if length <= 0:
        raise DNAError("DNA length ต้อง > 0")
    return DNASpec(
        kind="stream",
        length=length,
        dna_seed=int(numbers[2]),
        mutation_rate=normalize_mutation_rate(numbers[1]),
        mutation_seeds=tuple(int(s) for s in numbers[3:]),
    )


# ---- decode: คืน gate array 0/1 (dna[0]=1 เสมอ) ---------------------------
@lru_cache(maxsize=32)
def _decode_cached(dna_code: str) -> tuple[int, ...]:
    """decode จริง — cache ได้เพราะ pure + deterministic ตาม invariant

    เก็บเป็น tuple (immutable) เพื่อให้ค่าที่ cache ไว้ถูกแก้จากภายนอกไม่ได้
    DNAError ไม่ถูก cache (lru_cache ไม่เก็บ exception) จึง fail closed ทุกครั้ง
    """
    spec = parse_dna_spec(dna_code)
    if spec.kind == "bypass":
        return (1,) * spec.length

    # stream: base PCG64 + multi-mutation flip (ตรงกับตัว encode)
    dna = np.random.default_rng(spec.dna_seed).integers(
        0, 2, size=spec.length).astype(np.int8)
    dna[0] = 1
    for seed in spec.mutation_seeds:
        mask = np.random.default_rng(seed).random(spec.length) < spec.mutation_rate
        dna[mask] = 1 - dna[mask]
        dna[0] = 1
    return tuple(dna.astype(int).tolist())


def decode_dna(dna_code: str) -> list[int]:
    if not isinstance(dna_code, str):
        raise DNAError(f"dna_code ผิดรูปแบบ: {dna_code!r}")
    return list(_decode_cached(dna_code))


def dna_fingerprint(dna_code: str) -> str:
    """Digest of the decoded gate array — not of the code that produced it.

    The gate array is the only derived artifact that decides every row, and it
    was the only one with no guard: config_hash covers the dna_code string and
    calendar_fingerprint covers the slot grid, so nothing noticed if the same
    string decoded to a different array. It can, because numpy documents that
    Generator "does not provide a version compatibility guarantee... the bit
    stream may change", and requirements let numpy move. Storing this on the
    chain makes that change fail closed instead of silently trading a different
    strategy under the same name.
    """
    return hashlib.sha256(
        ",".join(str(b) for b in _decode_cached(dna_code)).encode()
    ).hexdigest()[:16]
