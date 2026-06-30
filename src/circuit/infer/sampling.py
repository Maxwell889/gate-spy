"""Deterministic sample generation and simulation at word granularity."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .words import Word, expand_word_value, word_value

if TYPE_CHECKING:
    from ..circuit import Circuit


@dataclass(frozen=True)
class Sample:
    inputs: dict[str, int]
    outputs: dict[str, int]
    input_bits: dict[str, int]
    output_bits: dict[str, int]


def _interesting_values(width: int) -> list[int]:
    mask = (1 << width) - 1
    vals = [0, 1, 2, mask]
    if width > 1:
        vals.extend([(1 << (width - 1)) - 1, 1 << (width - 1)])
    return list(dict.fromkeys(v & mask for v in vals))


def _row_key(row: dict[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(row.items()))


def generate_input_rows(input_words: dict[str, Word], sample_num: int,
                        seed: int = 0) -> list[dict[str, int]]:
    """Generate zero/boundary/one-hot/random input word rows."""
    sample_num = max(1, sample_num)
    names = list(input_words)
    rows: list[dict[str, int]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()

    def add(row: dict[str, int]) -> None:
        key = _row_key(row)
        if key not in seen and len(rows) < sample_num:
            rows.append(dict(row))
            seen.add(key)

    add({name: 0 for name in names})

    for name in names:
        word = input_words[name]
        for val in _interesting_values(word.width):
            row = {n: 0 for n in names}
            row[name] = val
            add(row)
            if len(rows) >= sample_num:
                return rows

    if names:
        for bit_row in range(max(w.width for w in input_words.values())):
            row = {}
            for name, word in input_words.items():
                row[name] = (1 << bit_row) & word.mask if bit_row < word.width else 0
            add(row)
            if len(rows) >= sample_num:
                return rows

    rng = random.Random(seed)
    while len(rows) < sample_num:
        add({name: rng.randint(0, input_words[name].mask) for name in names})
    return rows


def simulate_samples(circuit: "Circuit",
                     input_words: dict[str, Word],
                     output_words: dict[str, Word],
                     sample_num: int = 256,
                     seed: int = 0,
                     rows: list[dict[str, int]] | None = None) -> list[Sample]:
    """Simulate *circuit* and return word-level input/output samples."""
    samples: list[Sample] = []
    for row in rows or generate_input_rows(input_words, sample_num, seed=seed):
        input_bits: dict[str, int] = {}
        for name, word in input_words.items():
            input_bits.update(expand_word_value(word, row.get(name, 0)))

        output_bit_values = circuit.simulate(input_bits)
        outputs = {
            name: word_value(word, output_bit_values)
            for name, word in output_words.items()
        }
        samples.append(Sample(
            inputs=dict(row),
            outputs=outputs,
            input_bits=input_bits,
            output_bits=output_bit_values,
        ))
    return samples
