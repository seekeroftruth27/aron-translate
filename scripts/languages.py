"""Which languages Áron can hear, caption and speak -- three different lists.

  HEARD     Soniox's realtime model (stt-rt-v5): what the preacher may speak. The
            same 60 are what Soniox's own translation ("none, fast" and the
            Router) can write, and the only languages a bilingual PAIR can use,
            since both sides of a pair are spoken in the room.
  CAPTION   What a translator model (Claude, GPT, Gemini, Grok) can write on the
            screen: HEARD plus the languages in LLM_ONLY, each measured before it
            went in (validation/results/2026-10-25-caption-languages/).
  VOICE     What each text-to-speech backend can say, per model. A caption
            language with no voice is shown and not spoken.

The lists were read from the providers on 2026-10-25: Soniox from its /v1/models
and /v1/tts-models endpoints, ElevenLabs from its models page (the API key here
lacks models_read). Codes are ISO 639-1 where one exists, else 639-3.
"""

# Soniox stt-rt-v5, in the API's order (alphabetical by English name).
HEARD = {
    "af": "Afrikaans", "sq": "Albanian", "ar": "Arabic", "az": "Azerbaijani", "eu": "Basque",
    "be": "Belarusian", "bn": "Bengali", "bs": "Bosnian", "bg": "Bulgarian", "ca": "Catalan",
    "zh": "Chinese (Simplified)", "hr": "Croatian", "cs": "Czech", "da": "Danish", "nl": "Dutch",
    "en": "English", "et": "Estonian", "fi": "Finnish", "fr": "French", "gl": "Galician",
    "de": "German", "el": "Greek", "gu": "Gujarati", "he": "Hebrew", "hi": "Hindi",
    "hu": "Hungarian", "id": "Indonesian", "it": "Italian", "ja": "Japanese", "kn": "Kannada",
    "kk": "Kazakh", "ko": "Korean", "lv": "Latvian", "lt": "Lithuanian", "mk": "Macedonian",
    "ms": "Malay", "ml": "Malayalam", "mr": "Marathi", "no": "Norwegian", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "pa": "Punjabi", "ro": "Romanian", "ru": "Russian",
    "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian", "es": "Spanish", "sw": "Swahili",
    "sv": "Swedish", "tl": "Tagalog", "ta": "Tamil", "te": "Telugu", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "cy": "Welsh",
}

# Caption-only languages: a translator model writes them, Soniox neither hears
# nor translates them. Only those some Model reached "rough" or better on are listed.
LLM_ONLY = {
    "am": "Amharic",
    "hy": "Armenian",
    "as": "Assamese",
    "ast": "Asturian",
    "ay": "Aymara",
    "bem": "Bemba",
    "bho": "Bhojpuri",
    "br": "Breton",
    "my": "Burmese",
    "yue": "Cantonese",
    "ceb": "Cebuano",
    "ny": "Chichewa",
    "zh-hant": "Chinese (Traditional)",
    "chk": "Chuukese",
    "dv": "Dhivehi",
    "luo": "Dholuo",
    "ee": "Ewe",
    "fo": "Faroese",
    "fj": "Fijian",
    "ff": "Fula",
    "ka": "Georgian",
    "gn": "Guarani",
    "ht": "Haitian Creole",
    "cnh": "Hakha Chin",
    "ha": "Hausa",
    "haw": "Hawaiian",
    "hil": "Hiligaynon",
    "hmn": "Hmong",
    "is": "Icelandic",
    "ig": "Igbo",
    "ilo": "Ilocano",
    "ga": "Irish",
    "jv": "Javanese",
    "quc": "K'iche'",
    "kac": "Kachin (Jingpho)",
    "km": "Khmer",
    "ki": "Kikuyu",
    "rw": "Kinyarwanda",
    "rn": "Kirundi",
    "kri": "Krio",
    "ku": "Kurdish (Kurmanji)",
    "ckb": "Kurdish (Sorani)",
    "ky": "Kyrgyz",
    "lo": "Lao",
    "la": "Latin",
    "ln": "Lingala",
    "lg": "Luganda",
    "lb": "Luxembourgish",
    "mai": "Maithili",
    "mg": "Malagasy",
    "mt": "Maltese",
    "mh": "Marshallese",
    "lus": "Mizo",
    "mn": "Mongolian",
    "mi": "Māori",
    "ne": "Nepali",
    "pcm": "Nigerian Pidgin",
    "nso": "Northern Sotho",
    "oc": "Occitan",
    "or": "Odia",
    "om": "Oromo",
    "ps": "Pashto",
    "qu": "Quechua",
    "ksw": "S'gaw Karen",
    "sm": "Samoan",
    "gd": "Scottish Gaelic",
    "st": "Sesotho",
    "tn": "Setswana",
    "sn": "Shona",
    "sd": "Sindhi",
    "si": "Sinhala",
    "so": "Somali",
    "su": "Sundanese",
    "tg": "Tajik",
    "tt": "Tatar",
    "tet": "Tetum",
    "bo": "Tibetan",
    "ti": "Tigrinya",
    "tpi": "Tok Pisin",
    "to": "Tongan",
    "ts": "Tsonga",
    "tk": "Turkmen",
    "tw": "Twi",
    "ug": "Uyghur",
    "uz": "Uzbek",
    "war": "Waray",
    "wo": "Wolof",
    "xh": "Xhosa",
    "yi": "Yiddish",
    "yo": "Yoruba",
    "zu": "Zulu",
}

CAPTION = {**HEARD, **LLM_ONLY}

# What each Model writes, measured 2026-10-25 on a real English sermon passage
# (validation/results/2026-10-25-caption-languages/): the app's own prompt and
# rolling history, one caption at a time; two judges from other families named
# the language and rated it. "natural": both said the right language at 4-5 of
# 5. "rough": both said the right language at 3 -- understandable, word for word
# (Haiku's German is here, as it has been on screens since launch). Below that,
# or in the wrong language (Haiku wrote S'gaw Karen as Spanish, Cebuano as
# Tagalog), a language is not offered with that Model.
MEASURED = {
    "gpt-6-sol": {
        "natural": set("""af am ar as ay az be bg bho bn bo br bs ca ceb chk ckb cs cy da de el en es
            et eu fa ff fi fj fo fr ga gd gl gu ha haw he hi hil hmn hr ht hu hy id ig
            ilo is it ja jv ka ki kk km kn ko ksw ku ky la lb lg ln lo lt luo lus lv mai
            mg mh mi mk ml mn mr ms mt my ne nl no nso ny om or pa pcm pl ps pt rn ro ru
            rw sd si sk sl sm sn so sq sr st su sv sw ta te tg th ti tk tl tn to tpi tr
            ts tt tw ug uk ur uz vi war wo xh yo yue zh zh-hant zu""".split()),
        "rough": set("""bem cnh dv ee gn kac kri oc qu quc tet yi""".split()),
    },
    "claude-haiku-4-5": {
        "natural": set("""af ar ca cy en es fr hi nl no pt ro ta zh""".split()),
        "rough": set("""as ast bg bn bs cs da de fa fi gl he hr ht hu hy id is it ja ko la lb lt lv
            mk ml mr ne or pa pcm pl ru sk sq sr sv sw th tr uk ur vi yue zh-hant""".split()),
    },
    "gemini-3.5-flash-lite": {
        "natural": set("""af ar as az bg bn bs da de el en es et fa fi fr gl gu he hr ht hu hy it ja
            kk kn ko ky lt lus lv mk ms nl pa pl pt ru sd sk sr sv ta te th tr uk ur uz
            vi zh zh-hant""".split()),
        "rough": set("""am ast be bem bo br ca ceb chk ckb cnh cs eu ff fj fo ga gd ha haw hi hmn id
            ig is jv ka km ku la lb lg ln lo luo ml mn mr mt my ne no nso ny oc om pcm
            ps qu ro rw si sl sm sn so sq st su sw tet tg ti tk tl to tpi ts tt tw wo yo
            zu""".split()),
    },
    # 2026-10-26, measured as gpt-6.1-terra-fast. Hebrew is rough by one judge (3)
    # and native by the other (5).
    "gpt-6.1-terra": {
        "natural": set("""af am ar as az be bg bho bn bo bs ca ceb cs cy da de el en es et eu fa fi fo fr ga gd gl
            gu ha haw hi hmn hr ht hu hy id ig ilo is it ja jv ka kk km kn ko kri ksw ky la lb lg ln
            lo lt lus lv mai mg mi mk ml mn mr ms mt my ne nl no nso ny oc om or pa pcm pl ps pt ro
            ru rw sd si sk sl sm sn so sq sr st sv sw ta te tet tg th ti tk tl tn to tr ts tt tw ug
            uk ur uz vi war wo xh yi yo yue zh zh-hant zu""".split()),
        "rough": set("""ast ay bem br ckb ee ff fj gn he hil ki ku luo qu rn su tpi""".split()),
    },
}
MODEL_ALIASES = {"gpt-6-sol-fast": "gpt-6-sol",   # the same model on a faster tier
                 "gpt-6.1-terra-fast": "gpt-6.1-terra"}

# The 29 every translator has captioned since launch (they were ElevenLabs v2's
# list). A Model not measured keeps exactly these; a measured one keeps them too,
# so nothing a church uses today disappears (Haiku's Malay measured below rough).
LAUNCH_29 = set("en es fr de it pt pl ro nl tr sv id ja zh ko da fi bg ru hr sk cs el hu hi ar "
                "uk ms ta".split())


def caption_languages(model):
    """Codes this Model may caption into. "none-fast" is Soniox's own translation."""
    if model in ("none", "none-fast"):
        return set(HEARD)
    m = MEASURED.get(MODEL_ALIASES.get(model, model))
    return LAUNCH_29 | (m["natural"] | m["rough"] if m else set())


def rough_languages(model):
    """The ones among them that are not natural with this Model: word for word
    (or, for a launch language kept by the rule above, below that)."""
    m = MEASURED.get(MODEL_ALIASES.get(model, model))
    return (caption_languages(model) - m["natural"]) if m else set()


def model_languages():
    """For /caps: {model: [codes]}, plus "default" for a Model not measured."""
    out = {"none-fast": sorted(HEARD), "default": sorted(LAUNCH_29)}
    for m in [*MEASURED, *MODEL_ALIASES]:
        out[m] = sorted(caption_languages(m))
    return out


# ---- voices ------------------------------------------------------------------
_SX_TTS = set("af ar az be bg bn bs ca cs cy da de el en es et eu fa fi fr gl gu he hi "
              "hr hu id is it ja kk kn ko lt lv mk ml mr ms nl no pa pl pt ro ru sk sl "
              "sq sr su sv sw ta te th tl tr uk ur uz vi zh".split())
# ElevenLabs lists Filipino as "fil"; it is the same written language as Soniox's "tl".
_EL_V3 = set("af ar hy as ast az be bn bs bg my yue ca ceb ny hr cs da nl en et tl fi fr "
             "gl ka de el gu ha he hi hu is id ga it ja jv kn kk ky ko lv ln lt lb mk ms "
             "ml mt zh mi mr mn ne no oc or ps fa pl pt pa ro ru sr sd sk sl so es sw sv "
             "tg ta te th tr uk ur uz vi cy yo".split())
_EL_V2 = set("ar bg zh hr cs da nl en tl fi fr de el hi id it ja ko ms pl pt ro ru sk es "
             "sv ta tr uk".split())
_EL_FLASH = _EL_V2 | {"hu", "no", "vi"}
VOICE = {
    "soniox": _SX_TTS,
    "elevenlabs": {
        "eleven_v4": _EL_V3, "eleven_v4_turbo": _EL_V3, "eleven_v3": _EL_V3,
        "eleven_multilingual_v2": _EL_V2,
        "eleven_flash_v2_5": _EL_FLASH, "eleven_turbo_v2_5": _EL_FLASH,
    },
}

# A caption code a voice knows under another name.
VOICE_CODE = {"zh-hant": "zh"}


def voice_code(code):
    return VOICE_CODE.get(code, code)


def can_speak(provider, model, code):
    """True if this TTS backend + model can say `code`. An ElevenLabs model the
    table does not know is given the v4 list (the newest, widest one)."""
    langs = VOICE.get(provider)
    if isinstance(langs, dict):
        langs = langs.get(model or "", _EL_V3)
    return voice_code(code) in (langs or ())


def voice_languages():
    """The VOICE table as lists, for /caps."""
    return {p: ({m: sorted(s) for m, s in v.items()} if isinstance(v, dict) else sorted(v))
            for p, v in VOICE.items()}
