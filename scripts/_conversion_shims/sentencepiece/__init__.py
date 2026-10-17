"""Import-only stand-in for sentencepiece during llama.cpp GGUF conversion.

llama.cpp's Qwen converter imports sentencepiece before checking for a
``tokenizer.model``; Qwen3-VL ships a BPE ``tokenizer.json`` instead, so the
converter then falls back to its GPT-2/BPE vocabulary path and never uses
sentencepiece.  This shim lets that import succeed without installing the
package and fails loudly if anything actually tries to use it.
"""


class SentencePieceProcessor:  # noqa: D401 - mirrors the real class name
    def __init__(self, *args, **kwargs):
        raise RuntimeError("sentencepiece shim: real sentencepiece is required for this model")
