"""o200k_base token counts, read from the copy shipped with the CLI (the hash tiktoken itself expects): no network."""

from __future__ import annotations

from pathlib import Path

import tiktoken
from tiktoken.load import load_tiktoken_bpe

VOCAB = Path(__file__).with_name("data") / "o200k_base.tiktoken"
VOCAB_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
_O200K_PARTS = (   # tiktoken_ext.openai_public.o200k_base
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""\p{N}{1,3}""",
    r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
    r"""\s*[\r\n]+""",
    r"""\s+(?!\S)""",
    r"""\s+""",
)
O200K_PAT = "|".join(_O200K_PARTS)
_encoding: list[tiktoken.Encoding] = []


def tokens(body: bytes) -> int:
    """A text's o200k_base tokens, as a model trained on it would see them."""
    if not _encoding:
        _encoding.append(tiktoken.Encoding("o200k_base", pat_str=O200K_PAT, special_tokens={},
                                           mergeable_ranks=load_tiktoken_bpe(str(VOCAB), expected_hash=VOCAB_SHA256)))
    return len(_encoding[0].encode_ordinary(body.decode("utf-8", "replace")))
