"""123: spoken numbers → digits after transcribe (Scribe writes them as words, often hyphenated)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import textrules as T, subtitles as S  # noqa: E402

R = S.load_rules()


def say(s):
    out = " ".join(w["word"] for w in T.clean_words([{"word": x, "start": i, "end": i + 0.5} for i, x in enumerate(s.split())], "en", R))
    return out


def test_scribe_hyphens_and_units():
    assert say("join my twenty-eight-day workout") == "join my 28-day workout"
    assert say("twenty-eight day challenge, a four-week plan") == "28 day challenge, a 4-week plan"
    assert say("four weeks, two weeks.") == "4 weeks, 2 weeks."
    assert say("women over forty-five") == "women over 45"


def test_ages_amounts_and_symbols():
    assert say("I am fifty-two years old") == "I am 52 years old"
    assert say("lost thirty pounds") == "lost 30 lbs"
    assert say("ten thousand steps, fifty percent") == "10,000 steps, 50%"
    assert say("two thousand twenty-six") == "2026"


def test_one_stays_a_word_unless_it_is_a_quantity():
    assert say("one of my friends, no one knows") == "one of my friends, no one knows"
    assert say("one week") == "1 week"
    assert say("Day one.") == "Day 1."
    assert say("wait a second") == "wait a sec"


def test_a_comma_ends_a_number():
    assert say("twenty-six, one hundred and twenty reps") == "26, 120 reps"


def test_timings_span_the_merged_words():
    w = T.clean_words([{"word": "twenty", "start": 1.0, "end": 1.3, "speaker": "s1"},
                       {"word": "eight", "start": 1.35, "end": 1.6}, {"word": "days", "start": 1.7, "end": 2.0}], "en", R)
    assert w[0]["word"] == "28" and w[0]["start"] == 1.0 and w[0]["end"] == 1.6 and w[0]["speaker"] == "s1"


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("✓", k)
    print(f"{n}/{n} passed")
