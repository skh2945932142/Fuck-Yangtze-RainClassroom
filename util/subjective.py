"""Shaping helpers for subjective / fill-blank answers before submission.

Kept free of LLM, OCR and config imports so the length rules can be unit
tested on their own (the ai module pulls in openai + opencv at import time).
"""

# Punctuation we are willing to cut after when a hard cap has to be applied.
SENTENCE_ENDS = ("。", "；", ";", "！", "？")


def cap_answer(items, hard_max):
    """Enforce a hard character ceiling on each answer item.

    An item that merely exceeds the *soft* target is returned untouched: the
    soft target only lives in the prompt, and cutting a sentence mid-thought
    reads worse than a slightly long answer. Once an item passes ``hard_max``
    it is cut down to that ceiling, preferring the last sentence ending that
    is still reasonably long (at least half the ceiling) over a blunt slice.
    """
    if hard_max <= 0:
        return list(items)

    capped = []
    for item in items:
        if len(item) <= hard_max:
            capped.append(item)
            continue
        cut = item[:hard_max]
        threshold = hard_max / 2
        for sep in SENTENCE_ENDS:
            index = cut.rfind(sep)
            if index + 1 >= threshold:
                cut = cut[:index + 1]
                break
        capped.append(cut)
    return capped
