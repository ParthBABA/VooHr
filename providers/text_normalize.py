"""Text normalization for TTS.

Two layers:

1. ``strip_unspoken_symbols`` — strip markdown/emoji/symbol noise that an
   LLM leaves in replies and that Deepgram Aura would skip, read literally,
   or occasionally glitch on.
2. ``humanize_numbers`` — convert quantities (integers, decimals,
   percentages, currency, ordinals) into spoken-word form while leaving
   identifiers (phone numbers, OTPs, long digit runs, alphanumeric codes)
   untouched so they are read digit-by-digit.

``prepare_text_for_speech`` chains them (markdown stripped first, then
numbers) for use by the TTS provider.

English, and any other language supported by the ``num2words`` library, is
handled by ``num2words``; Hindi (unsupported by ``num2words``) uses the
built-in Indian-numbering converter below.
"""

import re

from num2words import num2words

__all__ = ["humanize_numbers", "strip_unspoken_symbols", "prepare_text_for_speech"]

# ── Masking placeholders ────────────────────────────────────────────────
# Identifiers are replaced with digit-free placeholders *before* number
# conversion (so no number regex can match them) and restored afterward.
_PLACEHOLDER_PREFIX = "\x00PH"
_PLACEHOLDER_SUFFIX = "\x00"


def _letter_index(idx: int) -> str:
    """Encode a counter as letters (a, b, ..., z, aa, ab, ...) — no digits."""
    out = []
    i = idx
    while True:
        out.append(chr(97 + (i % 26)))
        i = i // 26 - 1
        if i < 0:
            break
    return "".join(reversed(out))


def _make_placeholder(idx: int) -> str:
    return _PLACEHOLDER_PREFIX + _letter_index(idx) + _PLACEHOLDER_SUFFIX


# Phone numbers (international / US- / Indian-formatted), OTPs and long digit
# runs (6+), and alphanumeric codes/IDs such as EMP1023, INV-4521, TCKT-88.
_IDENTIFIER_RE = re.compile(
    r"\+\d{1,3}[\s.-]?\d{4,5}[\s.-]?\d{4,5}"      # +91-98765-43210
    r"|\d{3}[\s.-]\d{3}[\s.-]\d{4}"                # 987-654-3210
    r"|\d{4,5}[\s.-]\d{4,5}"                       # 98765-43210
    r"|\b\d{6,}\b"                                 # OTPs / long digit runs
    r"|\b[A-Za-z]{2,}[-_.]?\d{2,}\b"               # codes like EMP1023, INV-4521
)


def _mask_identifiers(text: str):
    tokens = []

    def _rep(match):
        idx = len(tokens)
        tokens.append(match.group(0))
        return _make_placeholder(idx)

    return _IDENTIFIER_RE.sub(_rep, text), tokens


def _restore(text: str, tokens) -> str:
    for idx, token in enumerate(tokens):
        text = text.replace(_make_placeholder(idx), token)
    return text


# ── Language resolution ──────────────────────────────────────────────────
def _resolve_lang(language_code: str) -> str:
    """Map a BCP-47 language code to a ``num2words`` lang code (or "hi")."""
    try:
        from num2words import CONVERTER_CLASSES
    except ImportError:  # pragma: no cover - defensive
        CONVERTER_CLASSES = {}

    code = (language_code or "").strip()
    parts = code.lower().split("-")
    two = parts[0] if parts else ""
    if not two:
        return "en"
    if two == "hi":
        return "hi"
    if two == "en" and len(parts) > 1 and parts[1].upper() == "IN":
        return "en_IN"  # Indian English (lakh/crore)
    if code in CONVERTER_CLASSES:
        return code
    if two in CONVERTER_CLASSES:
        return two
    if len(parts) > 1:
        alt = f"{two}_{parts[1].upper()}"
        if alt in CONVERTER_CLASSES:
            return alt
    return "en"


def _short(lang: str) -> str:
    if lang.startswith("hi"):
        return "hi"
    if lang.startswith("en"):
        return "en"
    return lang.split("_")[0]


def _n2w(value, lang: str, to: str = "cardinal") -> str:
    try:
        return num2words(value, lang=lang, to=to)
    except Exception:
        try:
            return num2words(value, lang="en", to=to)
        except Exception:  # pragma: no cover - defensive
            return str(value)


# ── Language-specific word tables ────────────────────────────────────────
_PERCENT_WORDS = {
    "hi": "प्रतिशत",
    "en": "percent",
    "es": "por ciento",
    "fr": "pour cent",
    "de": "prozent",
    "it": "per cento",
    "pt": "por cento",
}

_POINT_WORDS = {
    "hi": "दशमलव",
    "en": "point",
    "es": "punto",
    "fr": "virgule",
    "de": "Komma",
    "it": "virgola",
    "pt": "vírgula",
}

# symbol/code (lowercased) -> (english currency word, hindi currency word)
_CURRENCY_WORDS = {
    "₹": ("rupees", "रुपये"),
    "rs": ("rupees", "रुपये"),
    "rs.": ("rupees", "रुपये"),
    "inr": ("rupees", "रुपये"),
    "$": ("dollars", "डॉलर"),
    "usd": ("dollars", "डॉलर"),
    "€": ("euros", "यूरो"),
    "eur": ("euros", "यूरो"),
    "£": ("pounds", "पाउंड"),
    "gbp": ("pounds", "पाउंड"),
}

# Hindi numbers 0-99 are irregular, so they are spelled out in a table.
_HI_0_99 = [
    "शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह",
    "अठारह", "उन्नीस", "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस",
    "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस", "तीस", "इकतीस", "बत्तीस",
    "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस",
    "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चौवालीस", "पैंतालीस",
    "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास", "पचास", "इक्यावन", "बावन",
    "तिरपन", "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ", "साठ",
    "इकसठ", "बासठ", "तिरसठ", "चौंसठ", "पैंसठ", "छियासठ", "सरसठ", "अड़सठ",
    "उनहत्तर", "सत्तर", "इकहत्तर", "बहत्तर", "तिहत्तर", "चौहत्तर",
    "पचहत्तर", "छिहत्तर", "सतहत्तर", "अठहत्तर", "उन्यासी", "अस्सी",
    "इक्यासी", "बयासी", "तिरासी", "चौरासी", "पचासी", "छियासी", "सत्तासी",
    "अट्ठासी", "नवासी", "नब्बे", "इक्यानवे", "बानवे", "तिरानवे", "चौरानवे",
    "पचानवे", "छियानवे", "सत्तानवे", "अट्ठानवे", "निन्यानवे",
]

# Indian numbering system: groups of two digits (thousand, lakh, crore, ...).
_GROUP_NAMES = ["हज़ार", "लाख", "करोड़", "अरब", "खरब", "नील", "पद्म", "शंख"]


def _hi_hundreds(x: int) -> str:
    """Hindi words for 0 < x < 1000 (hundreds are compositional)."""
    if x <= 0:
        return ""
    if x < 100:
        return _HI_0_99[x]
    h, r = divmod(x, 100)
    word = f"{_HI_0_99[h]} सौ"
    if r:
        word += f" {_HI_0_99[r]}"
    return word


def _hi_int_words(n: int) -> str:
    """Hindi words for any non-negative integer using the Indian system."""
    if n == 0:
        return "शून्य"
    if n < 1000:
        return _hi_hundreds(n)

    s = str(n)
    chunks = []  # from the thousands group upward: chunks[0] = thousand
    rem = s[:-3]
    while rem:
        chunks.append(rem[-2:])
        rem = rem[:-2]

    parts = []
    for i in range(len(chunks) - 1, -1, -1):
        v = int(chunks[i])
        if v == 0:
            continue
        name = _GROUP_NAMES[i] if i < len(_GROUP_NAMES) else ""
        parts.append(f"{_hi_hundreds(v)}{' ' + name if name else ''}")

    hund = int(s[-3:])
    if hund:
        parts.append(_hi_hundreds(hund))

    return " ".join(p for p in parts if p.strip())


def _hi_number_words(val: str) -> str:
    if "." in val:
        whole, _, frac = val.partition(".")
        whole = whole or "0"
        frac = frac or "0"
        frac = "".join(d for d in frac if d.isdigit())
        digits = " ".join(_HI_0_99[int(d)] for d in frac)
        return f"{_hi_int_words(int(whole))} दशमलव {digits}"
    return _hi_int_words(int(val))


def _hi_ordinal(n: int) -> str:
    special = {
        1: "पहला", 2: "दूसरा", 3: "तीसरा", 4: "चौथा", 5: "पाँचवाँ", 6: "छठा",
        7: "सातवाँ", 8: "आठवाँ", 9: "नौवाँ", 10: "दसवाँ",
    }
    if n in special:
        return special[n]
    if n < 1000:
        base = _hi_hundreds(n)
    else:
        base = _hi_int_words(n)
    return base + "वाँ"


# ── Conversion helpers ───────────────────────────────────────────────────
def _number_words(raw: str, lang: str) -> str:
    val = raw.replace(",", "").strip()
    if lang.startswith("hi"):
        return _hi_number_words(val)
    if "." in val:
        whole, _, frac = val.partition(".")
        whole = whole or "0"
        frac = "".join(d for d in frac if d.isdigit()) or "0"
        point = _POINT_WORDS.get(_short(lang), "point")
        whole_word = _n2w(int(whole), lang)
        frac_words = [_n2w(int(d), lang) for d in frac]
        return f"{whole_word} {point} " + " ".join(frac_words)
    return _n2w(int(val), lang)


def _ordinal(n: int, lang: str) -> str:
    if lang.startswith("hi"):
        return _hi_ordinal(n)
    return _n2w(n, lang, to="ordinal")


# ── Step regexes (applied in order after identifier masking) ─────────────
_ORDINAL_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,4})(st|nd|rd|th)\b")
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9.,])(\d+(?:,\d{3})*(?:\.\d+)?)\s*%(?!\w)")
_CURRENCY_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:₹|Rs\.?|INR|USD|\$|€|EUR|£|GBP)\s*?)"
    r"(\d+(?:,\d{3})*(?:\.\d+)?)(?!\d)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:[.,]\d+)?(?![A-Za-z0-9])")


def _convert_ordinals(text: str, lang: str) -> str:
    return _ORDINAL_RE.sub(lambda m: _ordinal(int(m.group(1)), lang), text)


def _convert_percent(text: str, lang: str) -> str:
    pct = _PERCENT_WORDS.get(_short(lang), "percent")

    def _rep(m):
        return f"{_number_words(m.group(1), lang)} {pct}"

    return _PERCENT_RE.sub(_rep, text)


def _convert_currency(text: str, lang: str) -> str:
    hi = lang.startswith("hi")

    def _rep(m):
        sym = m.group(1).strip().lower()
        en_word, hi_word = _CURRENCY_WORDS.get(sym, ("rupees", "रुपये"))
        word = hi_word if hi else en_word
        return f"{_number_words(m.group(2), lang)} {word}"

    return _CURRENCY_RE.sub(_rep, text)


def _convert_numbers(text: str, lang: str) -> str:
    return _NUMBER_RE.sub(lambda m: _number_words(m.group(0), lang), text)


# ── Public entry point ───────────────────────────────────────────────────
# ── Symbol stripping (ISSUE 2) ───────────────────────────────────────────
# Markdown inline constructs: keep the inner text, drop the markers/syntax.
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*([^*\n]+?)\*(?!\w)")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)")

# Leading markdown markers (line start, multiline).
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[-*•][ \t]+", re.MULTILINE)
_NUMBERED_ITEM_RE = re.compile(r"^[ \t]*\d{1,4}[.)][ \t]+", re.MULTILINE)

# Emoji / pictographs / dingbats / flags / modifiers / variation selectors.
_EMOJI_RANGES = (
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"   # misc symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map symbols
    "\U0001F700-\U0001F8FF"   # alchemical / shapes / arrows ext
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FAFF"   # chess / symbols extended-A
    "\U00002600-\U000026FF"   # misc symbols (weather, dingbats)
    "\U00002700-\U000027BF"   # dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicator symbols (flags)
    "\U0001F3FB-\U0001F3FF"   # emoji skin-tone modifiers
    "\U0000FE00-\U0000FE0F"   # variation selectors
)
_EMOJI_RE = re.compile("[" + _EMOJI_RANGES + "]")


def _lang_symbol_tables(language_code: str):
    short = _short(_resolve_lang(language_code))
    if short == "hi":
        return {"and": "और", "at": "ऐट", "number": "नंबर ", "range": "से"}
    return {"and": "and", "at": "at", "number": "number ", "range": "to"}


# Remaining punctuation/symbols that carry no spoken value: remove outright.
# Includes zero-width joiners / direction marks left over from stripping
# multi-codepoint emoji.
_NOISE_RE = re.compile(r"[*_~^`|<>{}[\]\\#@\u200c\u200d\u200e\u200f\u2060\ufeff]")


def strip_unspoken_symbols(text: str, language_code: str = "en") -> str:
    """Remove markdown/symbol noise from LLM-generated text before TTS.

    Handles, in order:
      1. Markdown inline (keep inner text): ``[text](url)``, `` `code` ``,
         ``**bold**``/``__bold__``, ``*italic*``/``_italic_``,
         ``~~strike~~``, plus leading headers / bullets / numbered items.
      2. Emoji, pictographs, dingbats, flags (no spoken meaning).
      3. Symbols with spoken meaning, per language: ``&`` -> and,
         ``@`` (word-to-word) -> at, ``#123`` -> number 123, and a hyphen
         range ``10-15`` -> "10 to 15".
      4. Any remaining no-value symbols (``* _ ~ ^ ` | < > { } [ ] \\``).
      5. Collapse leftover whitespace and blank lines.

    ``%`` and currency characters (``$ € £ ₹``) are left intact so the number
    pipeline can convert them afterward.
    """
    if text is None:
        return ""
    text = str(text)
    if not text:
        return text

    words = _lang_symbol_tables(language_code)

    # 1) Markdown inline / leading markers — keep inner text.
    text = _LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"\1", text)
    # Leading markers last so a stray "-" bullet is only removed at line start.
    text = _HEADER_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _NUMBERED_ITEM_RE.sub("", text)

    # 2) Emoji / pictographs / flags — drop entirely.
    text = _EMOJI_RE.sub("", text)

    # 3) Symbols that carry spoken meaning.
    text = re.sub(r"&", " " + words["and"] + " ", text)
    text = re.sub(r"(?<=\w)@(?=\w)", " " + words["at"] + " ", text)
    # "#" immediately before digits reads as "number ".
    text = re.sub(r"#(?=\d)", words["number"], text)
    # Range dash between two numbers: 10-15 / 10 – 15 / 10–15.
    text = re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", " " + words["range"] + " ", text)

    # 4) Remaining no-value symbols.
    text = _NOISE_RE.sub("", text)

    # 5) Collapse whitespace and blank lines.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def prepare_text_for_speech(text: str, language_code: str = "en") -> str:
    """Normalize text for TTS: strip symbols first, then humanize numbers.

    Order matters — markdown/symbols must be removed *before* number
    conversion so something like ``**72%**`` isn't thrown off by leftover
    asterisks when the percent/number pass runs.
    """
    if text is None:
        return ""
    cleaned = strip_unspoken_symbols(str(text), language_code)
    return humanize_numbers(cleaned, language_code)


def humanize_numbers(text: str, language_code: str = "en") -> str:
    """Rewrite quantities in *text* into spoken-word form for TTS.

    Identifiers (phone numbers, OTPs / 6+ digit runs, alphanumeric codes)
    are masked beforehand and restored unchanged, so they keep being read
    digit-by-digit by the TTS engine.
    """
    if text is None:
        return ""
    text = str(text)
    if not text:
        return text

    lang = _resolve_lang(language_code)
    masked, tokens = _mask_identifiers(text)
    processed = _convert_ordinals(masked, lang)
    processed = _convert_percent(processed, lang)
    processed = _convert_currency(processed, lang)
    processed = _convert_numbers(processed, lang)
    return _restore(processed, tokens)


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - not all interpreters support it
        pass

    # ── Inline smoke tests ──────────────────────────────────────────────
    def check(label, got, want):
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}: {label}: {got!r}" + ("" if ok else f" (expected {want!r})"))
        return ok

    # English
    check("int", humanize_numbers("72", "en"), "seventy-two")
    check("decimal", humanize_numbers("8.5", "en-US"), "eight point five")
    check("percent", humanize_numbers("15%", "en"), "fifteen percent")
    check("currency Rs", humanize_numbers("Rs. 45000", "en"), "forty-five thousand rupees")
    check("currency -22645", humanize_numbers("₹45000", "en-IN"), "forty-five thousand rupees")
    check("currency usd", humanize_numbers("$500", "en"), "five hundred dollars")
    check("ordinals", humanize_numbers("the 1st and 3rd", "en"), "the first and third")

    # Identifiers stay untouched
    check("phone", humanize_numbers("Call 9876543210 now", "en"), "Call 9876543210 now")
    check("phone intl", humanize_numbers("+91-98765-43210", "en"), "+91-98765-43210")
    check("otp", humanize_numbers("OTP is 483920", "en"), "OTP is 483920")
    check("code", humanize_numbers("ID EMP1023 INV-4521", "en"), "ID EMP1023 INV-4521")

    # Hindi (custom converter, Indian numbering system)
    check("hi int", humanize_numbers("72", "hi-IN"), "बहत्तर")
    check("hi big", humanize_numbers("45000", "hi"), "पैंतालीस हज़ार")
    check("hi lakh", humanize_numbers("₹1,000,000", "hi"), "दस लाख रुपये")
    check("hi crore", humanize_numbers("₹10,000,000", "hi"), "एक करोड़ रुपये")
    check(
        "hi arab unit",
        _hi_int_words(1234567890),
        "एक अरब तेईस करोड़ पैंतालीस लाख सरसठ हज़ार आठ सौ नब्बे",
    )
    check("hi masked 10-digit", humanize_numbers("1234567890", "hi"), "1234567890")
    check("hi decimal", humanize_numbers("8.5", "hi-IN"), "आठ दशमलव पाँच")
    check("hi percent", humanize_numbers("15%", "hi"), "पंद्रह प्रतिशत")
    check("hi currency", humanize_numbers("₹45000", "hi-IN"), "पैंतालीस हज़ार रुपये")
    check(
        "hi mixed sentence",
        humanize_numbers("मेरे पास 72 रुपये और OTP 483920 है", "hi"),
        "मेरे पास बहत्तर रुपये और OTP 483920 है",
    )

    print("\n── Edge cases (Q3) ─────────────────────────────────────────────")
    # Numbers immediately followed by punctuation, no space
    check("punct period", humanize_numbers("72.", "en"), "seventy-two.")
    check("punct bang", humanize_numbers("72!", "en"), "seventy-two!")
    check("punct paren", humanize_numbers("(72)", "en"), "(seventy-two)")
    check("punct close", humanize_numbers("72) and 8.5", "en"), "seventy-two) and eight point five")
    check("punct percent", humanize_numbers("30%. ", "en"), "thirty percent. ")

    # Multiple numbers in one sentence
    check(
        "multi numbers",
        humanize_numbers("we have 3, 4 and 5 items", "en"),
        "we have three, four and five items",
    )
    check(
        "multi + currency",
        humanize_numbers("It costs Rs. 500 and 15% tax", "en"),
        "It costs five hundred rupees and fifteen percent tax",
    )
    check(
        "multi ordinals",
        humanize_numbers("The 1st, 2nd, and 3rd place.", "en"),
        "The first, second, and third place.",
    )
    check(
        "multi + identifier",
        humanize_numbers("call 9876543210 and 72% done", "en"),
        "call 9876543210 and seventy-two percent done",
    )
    check(
        "multi + code",
        humanize_numbers("Price is $72 and ID EMP1023 stays.", "en"),
        "Price is seventy-two dollars and ID EMP1023 stays.",
    )

    # Mixed English + Hindi in a single string (language drives conversion,
    # Latin digits/IDs and Latin word tokens are handled as before)
    check(
        "mixed en+hi",
        humanize_numbers("हमारे पास 72 रुपये और 15% टैक्स है और code TCKT-88।", "hi"),
        "हमारे पास बहत्तर रुपये और पंद्रह प्रतिशत टैक्स है और code TCKT-88।",
    )
    check(
        "mixed translit",
        humanize_numbers("सर्वर downtime 72 घंटे", "hi"),
        "सर्वर downtime बहत्तर घंटे",
    )
    check(
        "mixed decimal+code",
        humanize_numbers("Score 3.14 and code AB-12", "en"),
        "Score three point one four and code AB-12",
    )

    # No stray placeholder characters may survive in any output
    for label, text, lang in [
        ("leak-phone", "5 and EMP1023 done", "en"),
        ("leak-otp", "OTP 483920 arrived", "en"),
        ("leak-empty", "", "en"),
    ]:
        out = humanize_numbers(text, lang)
        if "\x00" in out:
            print(f"FAIL: {label}: placeholder leaked -> {out!r}")
        else:
            print(f"PASS: {label}: no placeholder leaked")

    print("\n── Symbol stripping (ISSUE 2) ──────────────────────────────────")
    check("markdown bold", strip_unspoken_symbols("**bold** and __b__", "en"), "bold and b")
    check("markdown italic", strip_unspoken_symbols("*ital* and _it2_", "en"), "ital and it2")
    check("markdown strike", strip_unspoken_symbols("~~gone~~", "en"), "gone")
    check("markdown code", strip_unspoken_symbols("`code` now", "en"), "code now")
    check("markdown link", strip_unspoken_symbols("[click here](https://x.com)", "en"), "click here")
    check("markdown header", strip_unspoken_symbols("## Section title", "en"), "Section title")
    check(
        "markdown bullets",
        strip_unspoken_symbols("- one\n* two\n1. three\n2. four", "en"),
        "one\ntwo\nthree\nfour",
    )
    check("emoji", strip_unspoken_symbols("go 😀🚀🇮🇳", "en"), "go")
    check("zwj emoji", strip_unspoken_symbols("👨\u200d👩\u200d👧 family", "en"), "family")
    check("amp", strip_unspoken_symbols("A & B", "en"), "A and B")
    check("at word", strip_unspoken_symbols("user@example.com", "en"), "user at example.com")
    check("hash number", strip_unspoken_symbols("issue #123", "en"), "issue number 123")
    check("hash tag", strip_unspoken_symbols("#hashtag", "en"), "hashtag")
    check("range dash", strip_unspoken_symbols("pages 10-15", "en"), "pages 10 to 15")
    check("range dash spaced", strip_unspoken_symbols("20 – 25", "en"), "20 to 25")
    check("symbols removed", strip_unspoken_symbols("A | B < C > D {x} \\ y", "en"), "A B C D x y")

    # Symbol stripping in Hindi
    check("hi amp", strip_unspoken_symbols("A & B", "hi"), "A और B")
    check("hi hashtag", strip_unspoken_symbols("विकल्प #7", "hi"), "विकल्प नंबर 7")

    # prepare_text_for_speech chains stripping then number conversion
    check("prep bold percent", prepare_text_for_speech("**72%** raise", "en"), "seventy-two percent raise")
    check(
        "prep range+amp",
        prepare_text_for_speech("pages 10-15 and A & B", "en"),
        "pages ten to fifteen and A and B",
    )
    check(
        "prep markdown numbers",
        prepare_text_for_speech("## Step 1: fix 3 bugs", "en"),
        "Step one: fix three bugs",
    )

    print("\n── Number regex regression (ISSUE 2) ───────────────────────────")
    # Trailing punctuation must NOT be swallowed by the number matcher.
    check("comma not swallowed", humanize_numbers("15, salary hike", "en"), "fifteen, salary hike")
    check("period not swallowed", humanize_numbers("Total is 42.", "en"), "Total is forty-two.")
    check("semicolon not swallowed", humanize_numbers("Error 500; retry", "en"), "Error five hundred; retry")
    check(
        "multi numbers kept",
        humanize_numbers("we have 3, 4 and 5 items", "en"),
        "we have three, four and five items",
    )
    check(
        "numbers inside markdown stripped",
        prepare_text_for_speech("**15%**", "en"),
        "fifteen percent",
    )