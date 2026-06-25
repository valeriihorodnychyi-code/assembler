"""Localization orchestration: dub -> re-transcribe -> caption, per language.

The elegant part of our pipeline: a localized clip goes through the SAME path as
the original. We don't try to translate word timings — we dub the audio, then
re-transcribe the dubbed media to get accurate target-language timings, then render
captions in the same style. Works identically whether the localized media comes from
ElevenLabs (here) or, later, a lip-sync provider (HeyGen/Sync) — we just feed the MP4.
"""
import os
import shutil
import tempfile

from . import dub, heygen, transcribe, compose, styles as st


def dub_and_transcribe(video_path, target_langs, dest_dir, source_lang="en",
                       api_key=None, transcribe_engine="whisper", model_size="small",
                       work_dir=None, progress=None, provider="elevenlabs", provider_key=None,
                       name_prefix=""):
    """Stage 1 of localization: produce a clean (no-caption) dubbed clip + its
    transcription per language. Returns (results, errors) where each result is
    {"lang", "clip"(filename in dest_dir), "words", "language"}.

    Captioning is intentionally a SEPARATE step so the editor can review/fix the
    dubbed text (or grab the caption-free clip) before burning subtitles.
    """
    os.makedirs(dest_dir, exist_ok=True)
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_dub_")
    provider_mod = heygen if provider == "heygen" else dub  # provider-agnostic localized media
    results, errors = [], {}
    for lang in target_langs:
        try:
            if progress:
                progress(lang, "dubbing")
            dubbed = provider_mod.dub_clip(video_path, lang, source_lang=source_lang,
                                           api_key=(provider_key or api_key), work_dir=work_dir)
            out_name = f"dub_{name_prefix}{lang}.mp4"
            dest = os.path.join(dest_dir, out_name)
            shutil.move(dubbed, dest)
            if progress:
                progress(lang, "transcribing")
            tr = transcribe.transcribe(dest, engine=transcribe_engine,
                                       model_size=model_size, language=lang, api_key=api_key)
            results.append({"lang": lang, "clip": out_name,
                            "words": tr["words"], "language": tr["language"]})
        except Exception as e:
            errors[lang] = str(e)
    return results, errors


def localize_clip(video_path, target_langs, style, formats, output_dir,
                  work_dir=None, source_lang="en", api_key=None,
                  transcribe_engine="whisper", model_size="small",
                  glossaries=None, smart_trim=False, progress=None):
    """Localize one clip into several languages.

    `style`      : a single engine-style dict (the look to keep across languages).
    `glossaries` : optional {lang: {term: replacement}} brand-term locks per language.
    Returns {lang: [output_paths]} and never raises per-language — a failed language
    is recorded under results['_errors'] so a batch keeps going.
    """
    os.makedirs(output_dir, exist_ok=True)
    work_dir = work_dir or tempfile.mkdtemp(prefix="cs_loc_")
    glossaries = glossaries or {}
    style = st.normalize(style)
    results, errors = {}, {}

    for lang in target_langs:
        try:
            if progress:
                progress(lang, "dubbing")
            dubbed = dub.dub_clip(video_path, lang, source_lang=source_lang,
                                  api_key=api_key, work_dir=work_dir,
                                  progress=lambda m, _l=lang: progress and progress(_l, m))

            if progress:
                progress(lang, "transcribing")
            tr = transcribe.transcribe(dubbed, engine=transcribe_engine,
                                       model_size=model_size, language=lang,
                                       api_key=api_key)

            region_style = dict(style)
            region = {"start": 0, "end": None, "style": region_style,
                      "replacements": glossaries.get(lang, {})}

            if progress:
                progress(lang, "rendering")
            outs = compose.render(dubbed, tr["words"], [region], formats, output_dir,
                                  work_dir=work_dir, smart_trim=smart_trim,
                                  out_prefix=lang)
            results[lang] = outs
        except Exception as e:  # keep the batch alive
            errors[lang] = str(e)
            if progress:
                progress(lang, f"error: {e}")

    if errors:
        results["_errors"] = errors
    return results


def batch_localize(video_paths, target_langs, style, formats, output_root, **kwargs):
    """Localize many clips. Returns {clip_name: {lang: [paths]}}."""
    out = {}
    for vp in video_paths:
        name = os.path.splitext(os.path.basename(vp))[0]
        out[name] = localize_clip(vp, target_langs, style, formats,
                                  os.path.join(output_root, name), **kwargs)
    return out
