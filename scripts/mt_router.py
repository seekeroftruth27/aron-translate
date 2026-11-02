#!/usr/bin/env python3
"""Provider routing for the offline test harnesses (effort_sweep, the stress
batteries): translate()/translate_stream() dispatched by model name to
translate_claude, translate_openai, translate_xai or translate_gemini -- the same
routing rule app/server.py applies to live /translate requests, so a harness
result is representative of what the live path would do with that model.

Kept separate from the server (which inlines the same rule) so the harnesses
don't import Flask-adjacent code, and the server doesn't grow a dependency on a
test helper.
"""
import translate_claude as _tc
import translate_openai as _to
import translate_xai as _tx
import translate_gemini as _tg


def _backend(model):
    if model in _to.OPENAI_MODELS:
        return _to
    if _tx.is_xai_model(model):     # whole grok- namespace: the live catalogue
        return _tx                  # turns over, a hardcoded tuple does not
    if _tg.is_gemini_model(model):  # whole gemini- namespace, same reason
        return _tg
    return _tc


def translate(text, history=None, model=_tc.DEFAULT_MODEL,
              target_lang=_tc.DEFAULT_TARGET_LANG, **kw):
    return _backend(model).translate(text, history=history, model=model,
                                     target_lang=target_lang, **kw)


def translate_stream(text, history=None, model=_tc.DEFAULT_MODEL,
                     target_lang=_tc.DEFAULT_TARGET_LANG, **kw):
    return _backend(model).translate_stream(text, history=history, model=model,
                                            target_lang=target_lang, **kw)
